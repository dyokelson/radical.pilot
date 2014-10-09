#!/usr/bin/env python

"""
.. module:: radical.pilot.agent
   :platform: Unix
   :synopsis: A multi-core agent for RADICAL-Pilot.

.. moduleauthor:: Mark Santcroos <mark.santcroos@rutgers.edu>
"""

__copyright__ = "Copyright 2014, http://radical.rutgers.edu"
__license__   = "MIT"

import os
import copy
import math
import saga
import stat
import sys
import time
import errno
import Queue
import signal
import shutil
import pymongo
import optparse
import logging
import datetime
import hostlist
import tempfile
import traceback
import threading 
import subprocess
import multiprocessing

from bson.objectid import ObjectId
from operator import mul


# ----------------------------------------------------------------------------
# CONSTANTS
FREE                 = 'Free'
BUSY                 = 'Busy'

LAUNCH_METHOD_SSH           = 'SSH'
LAUNCH_METHOD_APRUN         = 'APRUN'
LAUNCH_METHOD_LOCAL         = 'LOCAL'
LAUNCH_METHOD_MPIRUN        = 'MPIRUN'
LAUNCH_METHOD_MPIRUN_RSH    = 'MPIRUN_RSH'
LAUNCH_METHOD_MPIRUN_DPLACE = 'MPIRUN_DPLACE'
LAUNCH_METHOD_MPIEXEC       = 'MPIEXEC'
LAUNCH_METHOD_POE           = 'POE'
LAUNCH_METHOD_IBRUN         = 'IBRUN'
LAUNCH_METHOD_DPLACE        = 'DPLACE'
LAUNCH_METHOD_RUNJOB        = 'RUNJOB'

MULTI_NODE_LAUNCH_METHODS = [
    LAUNCH_METHOD_IBRUN,
    LAUNCH_METHOD_MPIRUN,
    LAUNCH_METHOD_RUNJOB,
    LAUNCH_METHOD_MPIRUN_RSH,
    LAUNCH_METHOD_MPIRUN_DPLACE,
    LAUNCH_METHOD_POE,
    LAUNCH_METHOD_APRUN,
    LAUNCH_METHOD_MPIEXEC
]

LRMS_NAME_TORQUE = 'TORQUE'
LRMS_NAME_PBSPRO = 'PBSPRO'
LRMS_NAME_SLURM = 'SLURM'
LRMS_NAME_SGE = 'SGE'
LRMS_NAME_LSF = 'LSF'
LRMS_NAME_LOADLEVELER = 'LOADL'
LRMS_NAME_FORK = 'FORK'

COMMAND_CANCEL_PILOT        = "Cancel_Pilot"
COMMAND_CANCEL_COMPUTE_UNIT = "Cancel_Compute_Unit"
COMMAND_KEEP_ALIVE          = "Keep_Alive"
COMMAND_FIELD               = "commands"
COMMAND_TYPE                = "type"
COMMAND_ARG                 = "arg"

#
# Staging Action operators
#
COPY     = 'Copy'     # local cp
LINK     = 'Link'     # local ln -s
MOVE     = 'Move'     # local mv
TRANSFER = 'Transfer' # saga remote transfer TODO: This might just be a special case of copy

STAGING_AREA = 'staging_area'

# -----------------------------------------------------------------------------
# Common States
NEW                         = 'New'
PENDING                     = 'Pending'
DONE                        = 'Done'
CANCELED                    = 'Canceled'
FAILED                      = 'Failed'

# -----------------------------------------------------------------------------
# ComputePilot States
PENDING_LAUNCH              = 'PendingLaunch'
LAUNCHING                   = 'Launching'
PENDING_ACTIVE              = 'PendingActive'
ACTIVE                      = 'Active'

# -----------------------------------------------------------------------------
# ComputeUnit States
PENDING_EXECUTION           = 'PendingExecution'
SCHEDULING                  = 'Scheduling'
EXECUTING                   = 'Executing'

PENDING_INPUT_STAGING       = 'PendingInputStaging'  # These last 4 are not really states,
STAGING_INPUT               = 'StagingInput'         # as there are distributed entities enacting on them.
PENDING_OUTPUT_STAGING      = 'PendingOutputStaging' # They should probably just go,
STAGING_OUTPUT              = 'StagingOutput'        # and be turned into logging events.

#---------------------------------------------------------------------------
MAX_IO_LOGLENGTH            = 64*1024 # max number of unit out/err chars to push to db


##########################################################################
#
# BG/Q Config
BGQ_CORES_PER_NODE      = 16
BGQ_NODES_PER_BOARD     = 32 # NODE == Compute Card == Chip module
BGQ_BOARDS_PER_MIDPLANE = 16 # NODE BOARD == NODE CARD
BGQ_MIDPLANES_PER_RACK  = 2
#
##########################################################################


##########################################################################
#
# Default mapping = "ABCDE(T)"
#
# http://www.redbooks.ibm.com/redbooks/SG247948/wwhelp/wwhimpl/js/html/wwhelp.htm
#
BGQ_MAPPING = "ABCDE"
#
##########################################################################


##########################################################################
#
# Board labels (Rack, Midplane, Node)
#
BGQ_BOARD_LABELS = ['R', 'M', 'N']
#
##########################################################################


##########################################################################
#
# Dimensions of a (sub-)block
#
BGQ_DIMENSION_LABELS = ['A', 'B', 'C', 'D', 'E']
#
##########################################################################


##########################################################################
#
# Supported sub-block sizes (number of nodes).
# This influences the effectiveness of mixed-size allocations
# (and might even be a hard requirement from a topology standpoint).
#
# TODO: Do we actually need to restrict our sub-block sizes to this set?
#
BGQ_SUPPORTED_SUB_BLOCK_SIZES = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]
#
##########################################################################


##########################################################################
#
# Mapping of starting corners.
#
# "board" -> "node"
#
# Ordering: ['E', 'D', 'DE', etc.]
#
# TODO: Is this independent of the mapping?
#
BGQ_BLOCK_STARTING_CORNERS = {
    0:  0,
    4: 29,
    8:  4,
    12: 25
}
#
##########################################################################


##########################################################################
#
# Offsets into block structure
#
BGQ_BLOCK_INDEX  = 0
BGQ_BLOCK_COOR   = 1
BGQ_BLOCK_NAME   = 2
BGQ_BLOCK_STATUS = 3
#
##########################################################################


##########################################################################
#
# BG/Q Topology of Boards within a Midplane
#
BGQ_MIDPLANE_TOPO = {
    0: {'A':  4, 'B':  8, 'C':  1, 'D':  2},
    1: {'A':  5, 'B':  9, 'C':  0, 'D':  3},
    2: {'A':  6, 'B': 10, 'C':  3, 'D':  0},
    3: {'A':  7, 'B': 11, 'C':  2, 'D':  1},
    4: {'A':  0, 'B': 12, 'C':  5, 'D':  6},
    5: {'A':  1, 'B': 13, 'C':  4, 'D':  7},
    6: {'A':  2, 'B': 14, 'C':  7, 'D':  4},
    7: {'A':  3, 'B': 15, 'C':  6, 'D':  5},
    8: {'A': 12, 'B':  0, 'C':  9, 'D': 10},
    9: {'A': 13, 'B':  1, 'C':  8, 'D': 11},
    10: {'A': 14, 'B':  2, 'C': 11, 'D':  8},
    11: {'A': 15, 'B':  3, 'C': 10, 'D':  9},
    12: {'A':  8, 'B':  4, 'C': 13, 'D': 14},
    13: {'A':  9, 'B':  5, 'C': 12, 'D': 15},
    14: {'A': 10, 'B':  6, 'C': 15, 'D': 12},
    15: {'A': 11, 'B':  7, 'C': 14, 'D': 13},
    }
#
##########################################################################


##########################################################################
#
# Verify the midplane topology
#
def bgq_check_midplane_topo():
    for board in range(BGQ_BOARDS_PER_MIDPLANE):
        for dimension in 'ABCD':
            target = BGQ_MIDPLANE_TOPO[board][dimension]
            reverse = BGQ_MIDPLANE_TOPO[target][dimension]
            assert reverse == board, 'board:%d dimension:%s target:%d reverse:%d' % (board, dimension, target, reverse)
#
##########################################################################


##########################################################################
#
# BG/Q Topology of Nodes within a Board
#
BGQ_BOARD_TOPO = {
    0: {'A': 29, 'B':  3, 'C':  1, 'D': 12, 'E':  7},
    1: {'A': 28, 'B':  2, 'C':  0, 'D': 13, 'E':  6},
    2: {'A': 31, 'B':  1, 'C':  3, 'D': 14, 'E':  5},
    3: {'A': 30, 'B':  0, 'C':  2, 'D': 15, 'E':  4},
    4: {'A': 25, 'B':  7, 'C':  5, 'D':  8, 'E':  3},
    5: {'A': 24, 'B':  6, 'C':  4, 'D':  9, 'E':  2},
    6: {'A': 27, 'B':  5, 'C':  7, 'D': 10, 'E':  1},
    7: {'A': 26, 'B':  4, 'C':  6, 'D': 11, 'E':  0},
    8: {'A': 21, 'B': 11, 'C':  9, 'D':  4, 'E': 15},
    9: {'A': 20, 'B': 10, 'C':  8, 'D':  5, 'E': 14},
    10: {'A': 23, 'B':  9, 'C': 11, 'D':  6, 'E': 13},
    11: {'A': 22, 'B':  8, 'C': 10, 'D':  7, 'E': 12},
    12: {'A': 17, 'B': 15, 'C': 13, 'D':  0, 'E': 11},
    13: {'A': 16, 'B': 14, 'C': 12, 'D':  1, 'E': 10},
    14: {'A': 19, 'B': 13, 'C': 15, 'D':  2, 'E':  9},
    15: {'A': 18, 'B': 12, 'C': 14, 'D':  3, 'E':  8},
    16: {'A': 13, 'B': 19, 'C': 17, 'D': 28, 'E': 23},
    17: {'A': 12, 'B': 18, 'C': 16, 'D': 29, 'E': 22},
    18: {'A': 15, 'B': 17, 'C': 19, 'D': 30, 'E': 21},
    19: {'A': 14, 'B': 16, 'C': 18, 'D': 31, 'E': 20},
    20: {'A':  9, 'B': 23, 'C': 21, 'D': 24, 'E': 19},
    21: {'A':  8, 'B': 22, 'C': 20, 'D': 25, 'E': 18},
    22: {'A': 11, 'B': 21, 'C': 23, 'D': 26, 'E': 17},
    23: {'A': 10, 'B': 20, 'C': 22, 'D': 27, 'E': 16},
    24: {'A':  5, 'B': 27, 'C': 25, 'D': 20, 'E': 31},
    25: {'A':  4, 'B': 26, 'C': 24, 'D': 21, 'E': 30},
    26: {'A':  7, 'B': 25, 'C': 27, 'D': 22, 'E': 29},
    27: {'A':  6, 'B': 24, 'C': 26, 'D': 23, 'E': 28},
    28: {'A':  1, 'B': 31, 'C': 29, 'D': 16, 'E': 27},
    29: {'A':  0, 'B': 30, 'C': 28, 'D': 17, 'E': 26},
    30: {'A':  3, 'B': 29, 'C': 31, 'D': 18, 'E': 25},
    31: {'A':  2, 'B': 28, 'C': 30, 'D': 19, 'E': 24},
    }
#
##########################################################################


##########################################################################
#
# Verify the board topology
#
def bgq_check_board_topo():
    for board in range(BGQ_NODES_PER_BOARD):
        for dimension in BGQ_DIMENSION_LABELS:
            target = BGQ_BOARD_TOPO[board][dimension]
            reverse = BGQ_BOARD_TOPO[target][dimension]
            assert reverse == board, 'board:%d dimension:%s target:%d reverse:%d' % (board, dimension, target, reverse)
#
##########################################################################


##########################################################################
#
# Convert the board string as given by llq into a board structure
#
# E.g. 'R00-M1-N08,R00-M1-N09,R00-M1-N10,R00-M0-N11' =>
# [{'R': 0, 'M': 1, 'N': 8}, {'R': 0, 'M': 1, 'N': 9},
#  {'R': 0, 'M': 1, 'N': 10}, {'R': 0, 'M': 0, 'N': 11}]
#
def bgq_str2boards(boards_str):

    boards = boards_str.split(',')

    board_dict_list = []

    for board in boards:
        elements = board.split('-')

        board_dict = {}
        for l, e in zip(BGQ_BOARD_LABELS, elements):
            board_dict[l] = int(e.split(l)[1])

        board_dict_list.append(board_dict)

    return board_dict_list
#
##########################################################################


##########################################################################
#
# Convert the string as given by llq into a block shape structure:
#
# E.g. '1x2x3x4x5' => {'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5}
#
def bgq_str2shape(shape_str):

    # Get the lengths of the shape
    shape_lengths = shape_str.split('x', 4)

    shape_dict = {}
    for dim, length in zip(BGQ_DIMENSION_LABELS, shape_lengths):
        shape_dict[dim] = int(length)

    return shape_dict
#
##########################################################################


##########################################################################
#
# Convert location dict into a tuple string
# E.g. {'A': 1, 'C': 4, 'B': 1, 'E': 2, 'D': 4} => '(1,4,1,2,4)'
#
def bgq_loc2str(loc):
    return str(tuple(loc[dim] for dim in BGQ_DIMENSION_LABELS))
#
##########################################################################


##########################################################################
#
# Convert a shape dict into string format
#
# E.g. {'A': 1, 'C': 4, 'B': 1, 'E': 2, 'D': 4} => '1x4x1x2x4'
#
def bgq_shape2str(shape):

    shape_str = ''
    for l in BGQ_DIMENSION_LABELS:

        # Get the corresponding count
        shape_str += str(shape[l])

        # Add an 'x' behind all but the last label
        if l in BGQ_DIMENSION_LABELS[:-1]:
            shape_str += 'x'

    return shape_str
#
##########################################################################


##########################################################################
#
# Walk the block and return the node name for the given location
#
def bgq_nodename_by_loc(rack, midplane, board, node, location):

    for dim in BGQ_DIMENSION_LABELS:
        max_length = location[dim]

        cur_length = 0
        # Loop while we are not at the final depth
        while cur_length < max_length:

            if cur_length % 2 == 0:
                # If the current length is even,
                # we remain within the board,
                # and select the next node.
                node = BGQ_BOARD_TOPO[node][dim]
            else:
                # Otherwise we jump to another midplane.
                board = BGQ_MIDPLANE_TOPO[board][dim]

            # Increase the length for the next iteration
            cur_length += 1

    return 'R%.2d-M%.1d-N%.2d-J%.2d' % (rack, midplane, board, node)
#
##########################################################################


##########################################################################
#
# Return list of nodes that make up the block
#
# Format: [(index, location, nodename, status), (i, c, n, s), ...]
#
def bgq_get_block(rack, midplane, board, shape):

    nodes = []
    start_node = BGQ_BLOCK_STARTING_CORNERS[board]

    print 'Shape: %s' % shape

    index = 0

    for a in range(shape['A']):
        for b in range(shape['B']):
            for c in range(shape['C']):
                for d in range(shape['D']):
                    for e in range(shape['E']):
                        location = {'A': a, 'B': b, 'C': c, 'D': d, 'E':e}
                        nodename = bgq_nodename_by_loc(rack, midplane, board, start_node, location)
                        nodes.append([index, location, nodename, FREE])
                        index += 1
    return nodes
#
##########################################################################


##########################################################################
#
# Use block shape and board list to construct block structure
#
def bgq_shapeandboards2block(block_shape_str, boards_str):

    board_dict_list = bgq_str2boards(boards_str)
    print 'Board dict list:'
    for b in board_dict_list:
        print b

    # TODO: this assumes a single midplane block
    rack     = board_dict_list[0]['R']
    midplane = board_dict_list[0]['M']

    board_list = [entry['N'] for entry in board_dict_list]
    start_board = min(board_list)

    block_shape = bgq_str2shape(block_shape_str)

    return bgq_get_block(rack, midplane, start_board, block_shape)
#
##########################################################################


##########################################################################
#
# Construction of sub-block shapes based on overall block allocation.
#
def bgq_create_sub_block_shape_table(shape_str):

    # Convert the shape string into dict structure
    block_shape = bgq_str2shape(shape_str)

    # Dict to store the results
    table = {}

    # Create a sub-block dict with shape 1x1x1x1x1
    sub_block_shape = {l: 1 for l in BGQ_DIMENSION_LABELS}

    # Look over all the dimensions starting at the most right
    for dim in BGQ_MAPPING[::-1]:
        while True:

            # Calculate the number of nodes for the current shape
            num_nodes = reduce(mul, filter(lambda length: length != 0, sub_block_shape.values()))

            if num_nodes in BGQ_SUPPORTED_SUB_BLOCK_SIZES:
                table[num_nodes] = copy.copy(sub_block_shape)

            # Done with iterating this dimension
            if sub_block_shape[dim] >= block_shape[dim]:
                break

            # Increase the length in this dimension for the next iteration.
            sub_block_shape[dim] += 1

    return table
#
##########################################################################


##########################################################################
#
# Return the offset into the node list from a corner
#
# TODO: Can this be determined instead of searched?
#
def bgq_corner2offset(block, corner):
    offset = 0

    for e in block:
        if corner == e[BGQ_BLOCK_COOR]:
            return offset
        offset += 1

    return offset
#
##########################################################################


##########################################################################
#
# Follow coordinates to get the last node
#
def bgq_get_last_node(origin, shape):
    return {dim: origin[dim] + shape[dim] -1 for dim in BGQ_DIMENSION_LABELS}
#
##########################################################################


##########################################################################
#
# Return the number of nodes in a block
#
def bgq_block2num_nodes(block):
    return len(block)
#
##########################################################################


##########################################################################
#
# Allocate a sub-block within a block
# Currently only works with offset that are exactly the sub-block size
#
def bgq_alloc_sub_block(block, num_nodes):

    offset = 0
    # Iterate through all nodes with offset a multiple of the sub-block size
    while True:

        # Verify the assumption (needs to be an assert?)
        if offset % num_nodes != 0:
            print 'ERROR: Sub-block needs to start at correct offset!'
            exit()
            # TODO: If we want to workaround this, the coordinates need to overflow

        not_free = False
        # Check if all nodes from offset till offset+size are FREE
        for peek in range(num_nodes):
            if block[offset+peek][BGQ_BLOCK_STATUS] == BUSY:
                # Once we find the first BUSY node we can discard this attempt
                not_free = True
                break

        if not_free == True:
            # No success at this offset
            print "INFO: No free nodes found at this offset: %d"  % offset

            # If we weren't the last attempt, then increase the offset and iterate again.
            if offset + num_nodes < bgq_block2num_nodes(block):
                offset += num_nodes
                continue
            else:
                return

        else:
            # At this stage we have found a free spot!

            print "INFO: Free nodes found at this offset: %d" % offset

            # Then mark the nodes busy
            for peek in range(num_nodes):
                block[offset+peek][BGQ_BLOCK_STATUS] = BUSY

            return offset
#
##########################################################################


##########################################################################
#
# Return the number of nodes for the given block shape
#
def bgq_shape2num_nodes(shape):

    nodes = 1
    for dim in BGQ_DIMENSION_LABELS:
        nodes *= shape[dim]

    return nodes
#
##########################################################################


##########################################################################
#
# Alloc a number of cores
#
def bgq_alloc_cores(block, sub_block_shape_table, num_cores):

    print "INFO: Trying to allocate %d core(s)." % num_cores

    if num_cores % BGQ_CORES_PER_NODE:
        num_cores = int(math.ceil(num_cores / float(BGQ_CORES_PER_NODE))) * BGQ_CORES_PER_NODE
        print 'ERROR: core not a multiple of %d, increasing request to %d!' % \
              (BGQ_CORES_PER_NODE, num_cores)

    num_nodes = num_cores / BGQ_CORES_PER_NODE

    offset = bgq_alloc_sub_block(block, num_nodes)

    if offset is None:
        print 'WARNING: No reservation made'
        return

    corner = block[offset][BGQ_BLOCK_COOR]
    sub_block_shape = sub_block_shape_table[num_nodes]

    print 'Allocating sub-block of %d node(s) with dimensions %s at offset %d with corner %s.' % \
          (num_nodes, bgq_shape2str(sub_block_shape), offset, bgq_loc2str(corner))
    end = bgq_get_last_node(corner, sub_block_shape)
    print 'End location: %s' % bgq_loc2str(end)

    return corner, sub_block_shape
#
##########################################################################


##########################################################################
#
# Free up an allocation
#
def bgq_free_cores(block, corner, shape):

    # Number of nodes to free
    num_nodes = bgq_shape2num_nodes(shape)

    # Location of where to start freeing
    offset = bgq_corner2offset(block, corner)

    print "INFO: Freeing %d nodes starting at %d." % (num_nodes, offset)

    for peek in range(num_nodes):
        assert block[offset+peek][BGQ_BLOCK_STATUS] == BUSY,\
            'Block %d not Free!' % block[offset+peek]
        block[offset+peek][BGQ_BLOCK_STATUS] = FREE
#
##########################################################################


#---------------------------------------------------------------------------
#
start_time = time.time ()
def get_rusage () :

    import resource

    self_usage  = resource.getrusage (resource.RUSAGE_SELF)
    child_usage = resource.getrusage (resource.RUSAGE_CHILDREN)

    rtime = time.time () - start_time
    utime = self_usage.ru_utime  + child_usage.ru_utime
    stime = self_usage.ru_stime  + child_usage.ru_stime
    rss   = self_usage.ru_maxrss + child_usage.ru_maxrss

    return "real %3f sec | user %.3f sec | system %.3f sec | mem %.2f kB" \
         % (rtime, utime, stime, rss)

#---------------------------------------------------------------------------
#
def pilot_FAILED(mongo_p, pilot_uid, logger, message):
    """Updates the state of one or more pilots.
    """
    logger.error(message)      
    ts = datetime.datetime.utcnow()

    mongo_p.update({"_id": ObjectId(pilot_uid)}, 
        {"$pushAll": {"log"         : [message, get_rusage()]},
         "$push"   : {"statehistory": {"state": FAILED, "timestamp": ts}},
         "$set"    : {"state"       : FAILED,
                      "capability"  : 0,
                      "finished"    : ts}
        })

#---------------------------------------------------------------------------
#
def pilot_CANCELED(mongo_p, pilot_uid, logger, message):
    """Updates the state of one or more pilots.
    """
    logger.warning(message)
    ts = datetime.datetime.utcnow()

    mongo_p.update({"_id": ObjectId(pilot_uid)}, 
        {"$pushAll": {"log"         : [message, get_rusage()]},
         "$push"   : {"statehistory": {"state": CANCELED, "timestamp": ts}},
         "$set"    : {"state"       : CANCELED,
                      "capability"  : 0,
                      "finished"    : ts}
        })

#---------------------------------------------------------------------------
#
def pilot_DONE(mongo_p, pilot_uid):
    """Updates the state of one or more pilots.
    """
    ts = datetime.datetime.utcnow()

    message = "pilot done"
    mongo_p.update({"_id": ObjectId(pilot_uid)}, 
        {"$pushAll": {"log"         : [message, get_rusage()]},
         "$push"   : {"statehistory": {"state": DONE, "timestamp": ts}},
         "$set"    : {"state"       : DONE,
                      "capability"  : 0,
                      "finished"    : ts}
        })


#-----------------------------------------------------------------------------
#
class ExecutionEnvironment(object):
    """DOC
    """
    #-------------------------------------------------------------------------
    #
    def __init__(self, logger, lrms_name, requested_cores, task_launch_method, mpi_launch_method):
        self.log = logger

        self.requested_cores = requested_cores
        self.node_list = None # TODO: Need to think about a structure that works for all machines
        self.cores_per_node = None # Work with one value for now

        # Configure nodes and number of cores available
        self.lrms = LRMS.factory(lrms_name, requested_cores, logger)

        self.task_launcher = LaunchMethod.factory(task_launch_method, logger)
        self.mpi_launcher = LaunchMethod.factory(mpi_launch_method, logger)

        self.discovered_launch_methods = {
            'task_launch_method': task_launch_method,
            'task_launch_command': self.task_launcher.launch_command,
            'mpi_launch_method': mpi_launch_method,
            'mpi_launch_command': self.mpi_launcher.launch_command
        }

        logger.info("Discovered task launch command: '%s' and MPI launch command: '%s'." % \
                    (self.task_launcher.launch_command, self.mpi_launcher.launch_command))

        logger.info("Discovered execution environment: %s" % self.lrms.node_list)

        # For now assume that all nodes have equal amount of cores
        cores_avail = len(self.lrms.node_list) * self.lrms.cores_per_node
        if cores_avail < int(requested_cores):
            raise Exception("Not enough cores available (%s) to satisfy allocation request (%s)." \
                            % (str(cores_avail), str(requested_cores)))


# ==============================================================================
#
# Launch Methods
#
# ==============================================================================
class LaunchMethod(object):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger):

        self.name = name
        self.log = logger

        self.launch_command = None
        self.configure()
        if self.launch_command is None:
            raise Exception("Launch command not found for method %s" % name)

    # This class-method creates the appropriate sub-class for the Launch Method.
    @classmethod
    def factory(cls, name, logger):

        # Make sure that we are the base-class!
        if cls != LaunchMethod:
            raise Exception("LaunchMethod Factory only available to base class!")

        implementations = {
            LAUNCH_METHOD_APRUN: LaunchMethodAPRUN,
            LAUNCH_METHOD_DPLACE: LaunchMethodDPLACE,
            LAUNCH_METHOD_IBRUN: LaunchMethodIBRUN,
            LAUNCH_METHOD_LOCAL: LaunchMethodLocal,
            LAUNCH_METHOD_MPIEXEC: LaunchMethodMPIEXEC,
            LAUNCH_METHOD_MPIRUN: LaunchMethodMPIRUN,
            LAUNCH_METHOD_MPIRUN_DPLACE: LaunchMethodMPIRUNDPLACE,
            LAUNCH_METHOD_MPIRUN_RSH: LaunchMethodMPIRUNRSH,
            LAUNCH_METHOD_POE: LaunchMethodPOE,
            LAUNCH_METHOD_RUNJOB: LaunchMethodRUNJOB,
            LAUNCH_METHOD_SSH: LaunchMethodSSH
        }
        try:
            return implementations[name](name, logger)
        except KeyError:
            raise Exception("LauncMethod '%s' unknown!" % name)

    # --------------------------------------------------------------------------
    #
    def configure(self):
        raise NotImplementedError("Configure not implemented for LaunchMethod: %s." % self.name)

    #-----------------------------------------------------------------------------
    #
    def _find_executable(self, names):
        """Takes a (list of) name(s) and looks for an executable in the path.
        """

        if not isinstance(names, list):
            names = [names]

        for name in names:
            ret = self._which(name)
            if ret is not None:
                return ret

        return None

    #-----------------------------------------------------------------------------
    #
    def _which(self, program):
        """Finds the location of an executable.
        Taken from: http://stackoverflow.com/questions/377017/test-if-executable-exists-in-python
        """
        #-------------------------------------------------------------------------
        #
        def is_exe(fpath):
            return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

        fpath, fname = os.path.split(program)
        if fpath:
            if is_exe(program):
                return program
        else:
            for path in os.environ["PATH"].split(os.pathsep):
                exe_file = os.path.join(path, program)
                if is_exe(exe_file):
                    return exe_file
        return None


#-------------------------------------------------------------------------
#
class LaunchMethodLocal(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # Regular tasks
        self.launch_command = ''


#-------------------------------------------------------------------------
#
class LaunchMethodMPIRUN(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        self.launch_command = self._find_executable([
            'mpirun',           # General case
            'mpirun_rsh',       # Gordon @ SDSC
            'mpirun-mpich-mp',  # Mac OSX MacPorts
            'mpirun-openmpi-mp' # Mac OSX MacPorts
        ]
    )


#-------------------------------------------------------------------------
#
class LaunchMethodSSH(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # Find ssh command
        command = self._which('ssh')

        if command is not None:

            # Some MPI environments (e.g. SGE) put a link to rsh as "ssh" into the path.
            # We try to detect that and then use different arguments.
            if os.path.islink(command):

                target = os.path.realpath(command)

                if os.path.basename(target) == 'rsh':
                    self.log.info('Detected that "ssh" is a link to "rsh".')
                    return target

            command = '%s -o StrictHostKeyChecking=no' % command

        self.launch_command = command


#-------------------------------------------------------------------------
#
class LaunchMethodMPIEXEC(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # mpiexec (e.g. on SuperMUC)
        self.launch_command = self._which('mpiexec')


#-------------------------------------------------------------------------
#
class LaunchMethodAPRUN(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # aprun: job launcher for Cray systems
        self.launch_command= self._which('aprun')


#-------------------------------------------------------------------------
#
class LaunchMethodRUNJOB(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # runjob: job launcher for IBM BG/Q systems, e.g. Joule
        self.launch_command= self._which('runjob')


#-------------------------------------------------------------------------
#
class LaunchMethodDPLACE(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # dplace: job launcher for SGI systems (e.g. on Blacklight)
        self.launch_command = self._which('dplace')


#-------------------------------------------------------------------------
#
class LaunchMethodMPIRUNRSH(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # mpirun_rsh (e.g. on Gordon@ SDSC)
        self.launch_command = self._which('mpirun_rsh')


#-------------------------------------------------------------------------
#
class LaunchMethodMPIRUNDPLACE(LaunchMethod):
    # TODO: This needs both mpirun and dplace

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # dplace: job launcher for SGI systems (e.g. on Blacklight)
        self.launch_command = self._which('dplace')


#-------------------------------------------------------------------------
#
class LaunchMethodIBRUN(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # ibrun: wrapper for mpirun at TACC
        self.launch_command = self._which('ibrun')


#-------------------------------------------------------------------------
#
class LaunchMethodPOE(LaunchMethod):

    def __init__(self, name, logger):
        LaunchMethod.__init__(self, name, logger)

    def configure(self):
        # poe: LSF specific wrapper for MPI (e.g. yellowstone)
        self.launch_command = self._which('poe')


# ==============================================================================
#
# Base class for LRMS implementations.
#
# ==============================================================================
#
class LRMS(object):

    def __init__(self, name, requested_cores, logger):

        print "Configuring LRMS %s." % name
        self.name = name
        self.log = logger
        self.requested_cores = requested_cores

        self.slot_list = []
        self.node_list = []
        self.cores_per_node = None

        self.configure()

    # This class-method creates the appropriate sub-class for the LRMS.
    @classmethod
    def factory(cls, name, requested_cores, logger):

        # TODO: Core counts dont have to be the same number for all hosts.

        # TODO: We might not have reserved the whole node.

        # TODO: Given that the Agent can determine the real core count, in principle we
        #       could just ignore the config and use as many as we have to our availability
        #       (taken into account that we might not have the full node reserved of course)
        #       Answer: at least on Yellowstone this doesnt work for MPI,
        #               as you can't spawn more tasks then the number of slots.

        # Make sure that we are the base-class!
        if cls != LRMS:
            raise Exception("LRMS Factory only available to base class!")

        implementations = {
            LRMS_NAME_TORQUE: TORQUELRMS,
            LRMS_NAME_PBSPRO: PBSProLRMS,
            LRMS_NAME_SLURM: SLURMLRMS,
            LRMS_NAME_SGE: SGELRMS,
            LRMS_NAME_LSF: LSFLRMS,
            LRMS_NAME_LOADLEVELER: LoadLevelerLRMS,
            LRMS_NAME_FORK: ForkLRMS
        }
        try:
            return implementations[name](name, requested_cores, logger)
        except KeyError:
            raise Exception("LRMS type '%s' unknown!" % name)

    def configure(self):
        raise NotImplementedError("Configure not implemented for LRMS type: %s." % self.name)


#-------------------------------------------------------------------------------
#
class TORQUELRMS(LRMS):

    def __init__(self, name, requested_cores, logger):
        LRMS.__init__(self, name, requested_cores, logger)

    def configure(self):

        self.log.info("Configured to run on system with %s." % self.name)

        torque_nodefile = os.environ.get('PBS_NODEFILE')
        if torque_nodefile is None:
            msg = "$PBS_NODEFILE not set!"
            self.log.error(msg)
            raise Exception(msg)

        # Parse PBS the nodefile
        torque_nodes = [line.strip() for line in open(torque_nodefile)]
        self.log.info("Found Torque PBS_NODEFILE %s: %s" % (torque_nodefile, torque_nodes))

        # Number of cpus involved in allocation
        val = os.environ.get('PBS_NCPUS')
        if val:
            torque_num_cpus = int(val)
        else:
            msg = "$PBS_NCPUS not set! (new Torque version?)"
            torque_num_cpus = None
            self.log.warning(msg)

        # Number of nodes involved in allocation
        val = os.environ.get('PBS_NUM_NODES')
        if val:
            torque_num_nodes = int(val)
        else:
            msg = "$PBS_NUM_NODES not set! (old Torque version?)"
            torque_num_nodes = None
            self.log.warning(msg)

        # Number of cores (processors) per node
        val = os.environ.get('PBS_NUM_PPN')
        if val:
            torque_cores_per_node = int(val)
        else:
            msg = "$PBS_NUM_PPN or $PBS_PPN not set!"
            torque_cores_per_node = None
            self.log.warning(msg)

        # Number of entries in nodefile should be PBS_NUM_NODES * PBS_NUM_PPN
        torque_nodes_length = len(torque_nodes)
        if torque_num_nodes and torque_cores_per_node and \
            torque_nodes_length != torque_num_nodes * torque_cores_per_node:
            msg = "Number of entries in $PBS_NODEFILE (%s) does not match with $PBS_NUM_NODES*$PBS_NUM_PPN (%s*%s)" % \
                  (torque_nodes_length, torque_num_nodes,  torque_cores_per_node)
            raise Exception(msg)

        # only unique node names
        torque_node_list = list(set(torque_nodes))
        torque_node_list_length = len(torque_node_list)
        self.log.debug("Node list: %s(%d)" % (torque_node_list, torque_node_list_length))

        if torque_num_nodes and torque_cores_per_node:
            # Modern style Torque
            self.cores_per_node = torque_cores_per_node
        elif torque_num_cpus:
            # Blacklight style (TORQUE-2.3.13)
            self.cores_per_node = torque_num_cpus
        else:
            # Old style Torque (Should we just use this for all versions?)
            self.cores_per_node = torque_nodes_length / torque_node_list_length
        self.node_list = torque_node_list


#-------------------------------------------------------------------------
#
class PBSProLRMS(LRMS):

    def __init__(self, name, requested_cores, logger):
        LRMS.__init__(self, name, requested_cores, logger)

    def configure(self):
        # TODO: $NCPUS?!?! = 1 on archer

        pbspro_nodefile = os.environ.get('PBS_NODEFILE')

        if pbspro_nodefile is None:
            msg = "$PBS_NODEFILE not set!"
            self.log.error(msg)
            raise Exception(msg)

        self.log.info("Found PBSPro $PBS_NODEFILE %s." % pbspro_nodefile)

        # Dont need to parse the content of nodefile for PBSPRO,
        # only the length is interesting, as there are only duplicate entries in it.
        pbspro_nodes_length = len([line.strip() for line in open(pbspro_nodefile)])

        # Number of Processors per Node
        val = os.environ.get('NUM_PPN')
        if val:
            pbspro_num_ppn = int(val)
        else:
            msg = "$NUM_PPN not set!"
            self.log.error(msg)
            raise Exception(msg)

        # Number of Nodes allocated
        val = os.environ.get('NODE_COUNT')
        if val:
            pbspro_node_count = int(val)
        else:
            msg = "$NODE_COUNT not set!"
            self.log.error(msg)
            raise Exception(msg)

        # Number of Parallel Environments
        val = os.environ.get('NUM_PES')
        if val:
            pbspro_num_pes = int(val)
        else:
            msg = "$NUM_PES not set!"
            self.log.error(msg)
            raise Exception(msg)

        pbspro_vnodes = self._parse_pbspro_vnodes()

        # Verify that $NUM_PES == $NODE_COUNT * $NUM_PPN == len($PBS_NODEFILE)
        if not (pbspro_node_count * pbspro_num_ppn == pbspro_num_pes == pbspro_nodes_length):
            self.log.warning("NUM_PES != NODE_COUNT * NUM_PPN != len($PBS_NODEFILE)")

        self.cores_per_node = pbspro_num_ppn
        self.node_list = pbspro_vnodes

    def _parse_pbspro_vnodes(self):

        # PBS Job ID
        val = os.environ.get('PBS_JOBID')
        if val:
            pbspro_jobid = val
        else:
            msg = "$PBS_JOBID not set!"
            self.log.error(msg)
            raise Exception(msg)

        # Get the output of qstat -f for this job
        output = subprocess.check_output(["qstat", "-f", pbspro_jobid])

        # Get the (multiline) 'exec_vnode' entry
        vnodes_str = ''
        for line in output.splitlines():
            # Detect start of entry
            if 'exec_vnode = ' in line:
                vnodes_str += line.strip()
            elif vnodes_str:
                # Find continuing lines
                if " = " not in line:
                    vnodes_str += line.strip()
                else:
                    break

        # Get the RHS of the entry
        input = vnodes_str.split('=',1)[1].strip()
        self.log.debug("input: %s" % input)

        nodes_list = []
        # Break up the individual node partitions into vnode slices
        while True:
            idx = input.find(')+(')

            node_str = input[1:idx]
            nodes_list.append(node_str)
            input = input[idx+2:]

            if idx < 0:
                break

        vnodes_list = []
        cpus_list = []
        # Split out the slices into vnode name and cpu count
        for node_str in nodes_list:
            slices = node_str.split('+')
            for slice in slices:
                vnode, cpus = slice.split(':')
                cpus = int(cpus.split('=')[1])
                self.log.debug('vnode: %s cpus: %s' % (vnode, cpus))
                vnodes_list.append(vnode)
                cpus_list.append(cpus)

        self.log.debug("vnodes: %s" % vnodes_list)
        self.log.debug("cpus: %s" % cpus_list)

        cpus_list = list(set(cpus_list))
        min_cpus = int(min(cpus_list))

        if len(cpus_list) > 1:
            self.log.debug("Detected vnodes of different sizes: %s, the minimal is: %d." % (cpus_list, min_cpus))

        node_list = []
        for vnode in vnodes_list:
            # strip the last _0 of the vnodes to get the node name
            node_list.append(vnode.rsplit('_', 1)[0])

        # only unique node names
        node_list = list(set(node_list))
        self.log.debug("Node list: %s" % node_list)

        # Return the list of node names
        return node_list


#-------------------------------------------------------------------------
#
class SLURMLRMS(LRMS):

    def __init__(self, name, requested_cores, logger):
        LRMS.__init__(self, name, requested_cores, logger)

    def configure(self):

        slurm_nodelist = os.environ.get('SLURM_NODELIST')
        if slurm_nodelist is None:
            msg = "$SLURM_NODELIST not set!"
            self.log.error(msg)
            raise Exception(msg)

        # Parse SLURM nodefile environment variable
        slurm_nodes = hostlist.expand_hostlist(slurm_nodelist)
        self.log.info("Found SLURM_NODELIST %s. Expanded to: %s" % (slurm_nodelist, slurm_nodes))

        # $SLURM_NPROCS = Total number of processes in the current job
        slurm_nprocs_str = os.environ.get('SLURM_NPROCS')
        if slurm_nprocs_str is None:
            msg = "$SLURM_NPROCS not set!"
            self.log.error(msg)
            raise Exception(msg)
        else:
            slurm_nprocs = int(slurm_nprocs_str)

        # $SLURM_NNODES = Total number of nodes in the job's resource allocation
        slurm_nnodes_str = os.environ.get('SLURM_NNODES')
        if slurm_nnodes_str is None:
            msg = "$SLURM_NNODES not set!"
            self.log.error(msg)
            raise Exception(msg)
        else:
            slurm_nnodes = int(slurm_nnodes_str)

        # $SLURM_CPUS_ON_NODE = Count of processors available to the job on this node.
        slurm_cpus_on_node_str = os.environ.get('SLURM_CPUS_ON_NODE')
        if slurm_cpus_on_node_str is None:
            msg = "$SLURM_NNODES not set!"
            self.log.error(msg)
            raise Exception(msg)
        else:
            slurm_cpus_on_node = int(slurm_cpus_on_node_str)

        # Verify that $SLURM_NPROCS == $SLURM_NNODES * $SLURM_CPUS_ON_NODE
        if slurm_nnodes * slurm_cpus_on_node != slurm_nprocs:
            self.log.error("$SLURM_NPROCS(%d) != $SLURM_NNODES(%d) * $SLURM_CPUS_ON_NODE(%d)" % \
                           (slurm_nnodes, slurm_cpus_on_node, slurm_nprocs))

        # Verify that $SLURM_NNODES == len($SLURM_NODELIST)
        if slurm_nnodes != len(slurm_nodes):
            self.log.error("$SLURM_NNODES(%d) != len($SLURM_NODELIST)(%d)" % \
                           (slurm_nnodes, len(slurm_nodes)))

        self.cores_per_node = slurm_cpus_on_node
        self.node_list = slurm_nodes

#-------------------------------------------------------------------------
#
class SGELRMS(LRMS):

    def __init__(self, name, requested_cores, logger):
        LRMS.__init__(self, name, requested_cores, logger)

    def configure(self):

        sge_hostfile = os.environ.get('PE_HOSTFILE')
        if sge_hostfile is None:
            msg = "$PE_HOSTFILE not set!"
            self.log.error(msg)
            raise Exception(msg)

        # SGE core configuration might be different than what multiprocessing announces
        # Alternative: "qconf -sq all.q|awk '/^slots *[0-9]+$/{print $2}'"

        # Parse SGE hostfile for nodes
        sge_node_list = [line.split()[0] for line in open(sge_hostfile)]
        # Keep only unique nodes
        sge_nodes = list(set(sge_node_list))
        self.log.info("Found PE_HOSTFILE %s. Expanded to: %s" % (sge_hostfile, sge_nodes))

        # Parse SGE hostfile for cores
        sge_cores_count_list = [int(line.split()[1]) for line in open(sge_hostfile)]
        sge_core_counts = list(set(sge_cores_count_list))
        sge_cores_per_node = min(sge_core_counts)
        self.log.info("Found unique core counts: %s Using: %d" % (sge_core_counts, sge_cores_per_node))

        self.node_list = sge_nodes
        self.cores_per_node = sge_cores_per_node


#-------------------------------------------------------------------------
#
class LSFLRMS(LRMS):

    def __init__(self, name, requested_cores, logger):
        LRMS.__init__(self, name, requested_cores, logger)

    def configure(self):

        lsf_hostfile = os.environ.get('LSB_DJOB_HOSTFILE')
        if lsf_hostfile is None:
            msg = "$LSB_DJOB_HOSTFILE not set!"
            self.log.error(msg)
            raise Exception(msg)

        lsb_mcpu_hosts = os.environ.get('LSB_MCPU_HOSTS')
        if lsb_mcpu_hosts is None:
            msg = "$LSB_MCPU_HOSTS not set!"
            self.log.error(msg)
            raise Exception(msg)

        # parse LSF hostfile
        # format:
        # <hostnameX>
        # <hostnameX>
        # <hostnameY>
        # <hostnameY>
        #
        # There are in total "-n" entries (number of tasks) and "-R" entries per host (tasks per host).
        # (That results in "-n" / "-R" unique hosts)
        #
        lsf_nodes = [line.strip() for line in open(lsf_hostfile)]
        self.log.info("Found LSB_DJOB_HOSTFILE %s. Expanded to: %s" % (lsf_hostfile, lsf_nodes))
        lsf_node_list = list(set(lsf_nodes))

        # Grab the core (slot) count from the environment
        # Format: hostX N hostY N hostZ N
        lsf_cores_count_list = map(int, lsb_mcpu_hosts.split()[1::2])
        lsf_core_counts = list(set(lsf_cores_count_list))
        lsf_cores_per_node = min(lsf_core_counts)
        self.log.info("Found unique core counts: %s Using: %d" % (lsf_core_counts, lsf_cores_per_node))

        self.node_list = lsf_node_list
        self.cores_per_node = lsf_cores_per_node


#-------------------------------------------------------------------------
#
class LoadLevelerLRMS(LRMS):

    def __init__(self, name, requested_cores, logger):
        LRMS.__init__(self, name, requested_cores, logger)

    def configure(self):

        # Determine method for determining hosts,
        # either through hostfile or BG/Q environment.
        loadl_hostfile = os.environ.get('LOADL_HOSTFILE')
        loadl_bg_block = os.environ.get('LOADL_BG_BLOCK')
        if loadl_hostfile is None and loadl_bg_block is None:
            msg = "Neither $LOADL_HOSTFILE or $LOADL_BG_BLOCK set!"
            self.log.error(msg)
            raise Exception(msg)

        # Determine the size of the pilot allocation
        if loadl_hostfile is not None:
            # MPICH?

            loadl_total_tasks_str = os.environ.get('LOADL_TOTAL_TASKS')
            if loadl_total_tasks_str is None:
                msg = "$LOADL_TOTAL_TASKS not set!"
                self.log.error(msg)
                raise Exception(msg)
            else:
                loadl_total_tasks = int(loadl_total_tasks_str)

            # Construct the host list
            loadl_nodes = [line.strip() for line in open(loadl_hostfile)]
            self.log.info("Found LOADL_HOSTFILE %s. Expanded to: %s" % (loadl_hostfile, loadl_nodes))
            loadl_node_list = list(set(loadl_nodes))

            # Verify that $LLOAD_TOTAL_TASKS == len($LOADL_HOSTFILE)
            if loadl_total_tasks != len(loadl_nodes):
                self.log.error("$LLOAD_TOTAL_TASKS(%d) != len($LOADL_HOSTFILE)(%d)" % \
                               (loadl_total_tasks, len(loadl_nodes)))

            # Determine the number of cpus per node
            # Assume: cores_per_node = lenght(nodefile) / len(unique_nodes_in_nodefile)
            loadl_cpus_per_node = len(loadl_nodes) / len(loadl_node_list)

        elif loadl_bg_block is not None:
            # Blue Gene?

            loadl_bg_size_str = os.environ.get('LOADL_BG_SIZE')
            if loadl_bg_size_str is None:
                msg = "$LOADL_BG_SIZE not set!"
                self.log.error(msg)
                raise Exception(msg)
            else:
                loadl_bg_size = int(loadl_bg_size_str)

            loadl_job_name = os.environ.get('LOADL_JOB_NAME')
            if loadl_job_name is None:
                msg = "$LOADL_JOB_NAME not set!"
                self.log.error(msg)
                raise Exception(msg)

            # Get the board list and block shape from 'llq -l' output
            output = subprocess.check_output(["llq", "-l", loadl_job_name])
            loadl_bg_board_list_str = None
            loadl_bg_block_shape_str = None
            for line in output.splitlines():
                # Detect BG board list
                if "BG Node Board List: " in line:
                    loadl_bg_board_list_str = line.split(':')[1].strip()
                elif "BG Shape Allocated: " in line:
                    loadl_bg_block_shape_str = line.split(':')[1].strip()
            if not loadl_bg_board_list_str:
                msg = "No board list found in llq output!"
                self.log.error(msg)
                raise Exception(msg)
            if not loadl_bg_block_shape_str:
                msg = "No board shape found in llq output!"
                self.log.error(msg)
                raise Exception(msg)

            # Build nodes data structure
            loadl_block = bgq_shapeandboards2block(loadl_bg_block_shape_str, loadl_bg_board_list_str)
            loadl_node_list = [entry[BGQ_BLOCK_NAME] for entry in loadl_block]

            # Determine the number of cpus per node
            loadl_cpus_per_node = BGQ_CORES_PER_NODE

        self.node_list = loadl_node_list
        self.cores_per_node = loadl_cpus_per_node


#-------------------------------------------------------------------------
#
class ForkLRMS(LRMS):

    def __init__(self, name, requested_cores, logger):
        LRMS.__init__(self, name, requested_cores, logger)

    def configure(self):

        self.log.info("Using fork on localhost.")

        detected_cpus = multiprocessing.cpu_count()
        selected_cpus = min(detected_cpus, self.requested_cores)

        self.log.info("Detected %d cores on localhost, using %d." % (detected_cpus, selected_cpus))

        self.node_list = ["localhost"]
        self.cores_per_node = selected_cpus



# ----------------------------------------------------------------------------
#
class Task(object):

    def __init__(self, uid, executable, arguments, environment, numcores, mpi,
                 pre_exec, post_exec, workdir, stdout_file, stderr_file, 
                 agent_output_staging, ftw_output_staging):

        self._log         = None
        self._description = None

        # static task properties
        self.uid            = uid
        self.environment    = environment
        self.executable     = executable
        self.arguments      = arguments
        self.workdir        = workdir
        self.stdout_file    = stdout_file
        self.stderr_file    = stderr_file
        self.agent_output_staging = agent_output_staging
        self.ftw_output_staging = ftw_output_staging
        self.numcores       = numcores
        self.mpi            = mpi
        self.pre_exec       = pre_exec
        self.post_exec      = post_exec

        # Location
        self.slots          = None

        # dynamic task properties
        self.started        = None
        self.finished       = None

        self.state          = None
        self.exit_code      = None
        self.stdout         = ""
        self.stderr         = ""

        self._log           = []
        self._proc          = None


# ----------------------------------------------------------------------------
#
class ExecWorker(multiprocessing.Process):
    """An ExecWorker competes for the execution of tasks in a task queue
    and writes the results back to MongoDB.
    """

    # ------------------------------------------------------------------------
    #
    def __init__(self, logger, task_queue, command_queue, output_staging_queue,
                 node_list, cores_per_node, launch_methods, mongodb_url, mongodb_name, mongodb_auth,
                 pilot_id, session_id, benchmark):

        """Le Constructeur creates a new ExecWorker instance.
        """
        multiprocessing.Process.__init__(self)
        self.daemon      = True
        self._terminate  = False

        self._log = logger

        self._pilot_id  = pilot_id
        self._benchmark = benchmark

        mongo_client = pymongo.MongoClient(mongodb_url)
        self._mongo_db = mongo_client[mongodb_name]

        if  len (mongodb_auth) >= 3 :
            user, pwd = mongodb_auth.split (':', 1)
            self._mongo_db.authenticate (user, pwd)

        self._p  = mongo_db["%s.p"  % session_id]
        self._cu = mongo_db["%s.cu" % session_id]
        self._wm = mongo_db["%s.um" % session_id]

        # Queued tasks by the Agent
        self._task_queue     = task_queue

        # Queued transfers
        self._output_staging_queue = output_staging_queue

        # Queued commands by the Agent
        self._command_queue = command_queue

        # Launched tasks by this ExecWorker
        self._running_tasks = []
        self._cuids_to_cancel = []

        # Slots represents the internal process management structure.
        # The structure is as follows:
        # [
        #    {'node': 'node1', 'cores': [p_1, p_2, p_3, ... , p_cores_per_node]},
        #    {'node': 'node2', 'cores': [p_1, p_2, p_3. ... , p_cores_per_node]
        # ]
        #
        # We put it in a list because we care about (and make use of) the order.
        #
        self._slots = []
        for node in node_list:
            self._slots.append({
                'node': node,
                # TODO: Maybe use the real core numbers in the case of non-exclusive host reservations?
                'cores': [FREE for _ in range(0, cores_per_node)]
            })
        self._cores_per_node = cores_per_node

        #self._capability = self._slots2caps(self._slots)
        self._capability     = self._slots2free(self._slots)
        self._capability_old = None

        # keep a slot allocation history (short status), start with presumably
        # empty state now
        self._slot_history     = [self._slot_status (short=True)]
        self._slot_history_old = None

        # The available launch methods
        self._available_launch_methods = launch_methods

        self._p.update(
            {"_id": ObjectId(self._pilot_id)},
            {"$set": {"slothistory" : self._slot_history,
                      "capability"  : 0,
                      "slots"       : self._slots}}
            )

    # ------------------------------------------------------------------------
    #
    def _slots2free(self, slots):
        """Convert slots structure into a free core count
        """

        free_cores = 0
        for node in slots:
            free_cores += node['cores'].count(FREE)

        return free_cores


    # ------------------------------------------------------------------------
    #
    def _slots2caps(self, slots):
        """Convert slots structure into a capability structure.
        """

        all_caps_tuples = {}
        for node in slots:
            free_cores = node['cores'].count(FREE)
            # (Free_cores, Continuous, Single_Node) = Count
            cap_tuple = (free_cores, False, True)

            if cap_tuple in all_caps_tuples:
                all_caps_tuples[cap_tuple] += 1
            else:
                all_caps_tuples[cap_tuple] = 1


        # Convert to please the gods of json and mongodb
        all_caps_dict = []
        for caps_tuple in all_caps_tuples:
            free_cores, cont, single = cap_tuple
            count = all_caps_tuples[cap_tuple]
            cap_dict = {'free_cores': free_cores, 'continuous': cont, 'single_node': single, 'count': count}
            all_caps_dict.append(cap_dict)

        return all_caps_dict

    # ------------------------------------------------------------------------
    #
    def stop(self):
        """Terminates the process' main loop.
        """
        # AM: Why does this call exist?  It is never called....
        self._terminate = True

    # ------------------------------------------------------------------------
    #
    def run(self):
        """Starts the process when Process.start() is called.
        """
        try:
            while self._terminate is False:

                idle = True

                self._log.debug("Slot status:\n%s", self._slot_status())

                # See if there are commands for the worker!
                try:
                    command = self._command_queue.get_nowait()
                    if command[COMMAND_TYPE] == COMMAND_CANCEL_COMPUTE_UNIT:
                        self._cuids_to_cancel.append(command[COMMAND_ARG])
                    else:
                        raise Exception("Command %s not applicable in this context." % command[COMMAND_TYPE])
                except Queue.Empty:
                    # do nothing if we don't have any queued commands
                    pass

                task = None
                try:
                    task = self._task_queue.get_nowait()

                except Queue.Empty:
                    # do nothing if we don't have any queued tasks
                    pass

                # any work to do?
                if  task :

                    task_slots = None

                    try :

                        if task.mpi:
                            launch_method = self._available_launch_methods['mpi_launch_method']
                            launch_command = self._available_launch_methods['mpi_launch_command']
                            if not launch_command:
                                raise Exception("Can't launch MPI tasks without MPI launcher.")
                        else:
                            launch_method = self._available_launch_methods['task_launch_method']
                            launch_command = self._available_launch_methods['task_launch_command']

                        self._log.debug("Launching task with %s (%s)." % (
                            launch_method, launch_command))

                        # IBRUN (e.g. Stampede) requires continuous slots for multi core execution
                        # TODO: Dont have scattered scheduler yet, so test disabled.
                        if True: # launch_method in [LAUNCH_METHOD_IBRUN]:
                            req_cont = True
                        else:
                            req_cont = False

                        # First try to find all cores on a single node
                        task_slots = self._acquire_slots(task.numcores, single_node=True, continuous=req_cont)

                        # If that failed, and our launch method supports multiple nodes, try that
                        if  task_slots is None and launch_method in MULTI_NODE_LAUNCH_METHODS:
                            task_slots = self._acquire_slots(task.numcores, single_node=False, continuous=req_cont)

                        # Check if we got results
                        if  task_slots is None:
                            # No resources free, put back in queue
                            self._task_queue.put(task)
                        else:
                            # We got an allocation go off and launch the process
                            task.slots = task_slots
                            self._launch_task(task, launch_method, launch_command)
                            idle = False

                    except Exception as e :
                        # append the startup error to the units stderr.  This is
                        # not completely correct (as this text is not produced
                        # by the unit), but it seems the most intuitive way to
                        # communicate that error to the application/user.
                        task.state   = FAILED
                        task.stderr += "\nPilot cannot start compute unit: '%s'" % e
                        
                        self._log.error ("Launching task failed: '%s'." % e)

                        # Free the Slots, Flee the Flots, Ree the Frots!
                        if  task_slots :
                            self._change_slot_states(task_slots, FREE)

                        self._update_tasks (task)


                # Record if there was activity in launching or monitoring tasks.
                idle &= self._check_running()

                # If nothing happened in this cycle, zzzzz for a bit.
                if idle:
                    self._log.debug("Sleep now for a jiffy ...")
                    time.sleep(0.1)

        except Exception, ex:
            msg = ("Error in ExecWorker loop: %s", traceback.format_exc())
            pilot_FAILED(self._p, self._pilot_id, self._log, msg)
            return


    # ------------------------------------------------------------------------
    #
    def _slot_status(self, short=False):
        """Returns a multi-line string corresponding to slot status.
        """

        if short:
            slot_matrix = ""
            for slot in self._slots:
                slot_matrix += "|"
                for core in slot['cores']:
                    if core is FREE:
                        slot_matrix += "-"
                    else:
                        slot_matrix += "+"
            slot_matrix += "|"
            ts = datetime.datetime.utcnow()
            return {'timestamp' : ts, 'slotstate' : slot_matrix}

        else :
            slot_matrix = ""
            for slot in self._slots:
                slot_vector  = ""
                for core in slot['cores']:
                    if core is FREE:
                        slot_vector += " - "
                    else:
                        slot_vector += " X "
                slot_matrix += "%s: %s\n" % (slot['node'].ljust(24), slot_vector)
            return slot_matrix


    # ------------------------------------------------------------------------
    #
    # Returns a data structure in the form of:
    #
    #
    def _acquire_slots(self, cores_requested, single_node, continuous):

        #
        # Find a needle (continuous sub-list) in a haystack (list)
        #
        def find_sublist(haystack, needle):
            n = len(needle)
            # Find all matches (returns list of False and True for every position)
            hits = [(needle == haystack[i:i+n]) for i in xrange(len(haystack)-n+1)]
            try:
                # Grab the first occurrence
                index = hits.index(True)
            except ValueError:
                index = None

            return index

        #
        # Transform the number of cores into a continuous list of "status"es,
        # and use that to find a sub-list.
        #
        def find_cores_cont(slot_cores, cores_requested, status):
            return find_sublist(slot_cores, [status for _ in range(cores_requested)])

        #
        # Find an available continuous slot within node boundaries.
        #
        def find_slots_single_cont(cores_requested):

            for slot in self._slots:
                slot_node = slot['node']
                slot_cores = slot['cores']

                slot_cores_offset = find_cores_cont(slot_cores, cores_requested, FREE)

                if slot_cores_offset is not None:
                    self._log.info('Node %s satisfies %d cores at offset %d' % (slot_node, cores_requested, slot_cores_offset))
                    return ['%s:%d' % (slot_node, core) for core in range(slot_cores_offset, slot_cores_offset + cores_requested)]

            return None

        #
        # Find an available continuous slot across node boundaries.
        #
        def find_slots_multi_cont(cores_requested):

            # Convenience aliases
            cores_per_node = self._cores_per_node
            all_slots = self._slots

            # Glue all slot core lists together
            all_slot_cores = [core for node in [node['cores'] for node in all_slots] for core in node]
          # self._log.debug("all_slot_cores: %s" % all_slot_cores)

            # Find the start of the first available region
            all_slots_first_core_offset = find_cores_cont(all_slot_cores, cores_requested, FREE)
            self._log.debug("all_slots_first_core_offset: %s" % all_slots_first_core_offset)
            if all_slots_first_core_offset is None:
                return None

            # Determine the first slot in the slot list
            first_slot_index = all_slots_first_core_offset / cores_per_node
            self._log.debug("first_slot_index: %s" % first_slot_index)
            # And the core offset within that node
            first_slot_core_offset = all_slots_first_core_offset % cores_per_node
            self._log.debug("first_slot_core_offset: %s" % first_slot_core_offset)

            # Note: We subtract one here, because counting starts at zero;
            #       Imagine a zero offset and a count of 1, the only core used would be core 0.
            #       TODO: Verify this claim :-)
            all_slots_last_core_offset = (first_slot_index * cores_per_node) + first_slot_core_offset + cores_requested - 1
            self._log.debug("all_slots_last_core_offset: %s" % all_slots_last_core_offset)
            last_slot_index = (all_slots_last_core_offset) / cores_per_node
            self._log.debug("last_slot_index: %s" % last_slot_index)
            last_slot_core_offset = all_slots_last_core_offset % cores_per_node
            self._log.debug("last_slot_core_offset: %s" % last_slot_core_offset)

            # Convenience aliases
            last_slot = self._slots[last_slot_index]
            self._log.debug("last_slot: %s" % last_slot)
            last_node = last_slot['node']
            self._log.debug("last_node: %s" % last_node)
            first_slot = self._slots[first_slot_index]
            self._log.debug("first_slot: %s" % first_slot)
            first_node = first_slot['node']
            self._log.debug("first_node: %s" % first_node)

            # Collect all node:core slots here
            task_slots = []

            # Add cores from first slot for this task
            # As this is a multi-node search, we can safely assume that we go from the offset all the way to the last core
            task_slots.extend(['%s:%d' % (first_node, core) for core in range(first_slot_core_offset, cores_per_node)])

            # Add all cores from "middle" slots
            for slot_index in range(first_slot_index+1, last_slot_index):
                slot_node = all_slots[slot_index]['node']
                task_slots.extend(['%s:%d' % (slot_node, core) for core in range(0, cores_per_node)])

            # Add the cores of the last slot
            task_slots.extend(['%s:%d' % (last_node, core) for core in range(0, last_slot_core_offset+1)])

            return task_slots

        #  End of inline functions, _acquire_slots() code begins after this
        #################################################################################

        #
        # Switch between searching for continuous or scattered slots
        #
        # Switch between searching for single or multi-node
        if single_node:
            if continuous:
                task_slots = find_slots_single_cont(cores_requested)
            else:
                raise NotImplementedError('No scattered single node scheduler implemented yet.')
        else:
            if continuous:
                task_slots = find_slots_multi_cont(cores_requested)
            else:
                raise NotImplementedError('No scattered multi node scheduler implemented yet.')

        if task_slots is not None:
            self._change_slot_states(task_slots, BUSY)

        return task_slots

    #
    # Change the reserved state of slots (FREE or BUSY)
    #
    # task_slots in the shape of:
    #
    #
    def _change_slot_states(self, task_slots, new_state):

        # Convenience alias
        all_slots = self._slots

      # logger.debug("change_slot_states: task slots: %s" % task_slots)

        for slot in task_slots:
          # logger.debug("change_slot_states: slot content: %s" % slot)
            # Get the node and the core part
            [slot_node, slot_core] = slot.split(':')
            # Find the entry in the the all_slots list
            slot_entry = (slot for slot in all_slots if slot["node"] == slot_node).next()
            # Change the state of the slot
            slot_entry['cores'][int(slot_core)] = new_state

        # something changed - write history!
        # AM: mongodb entries MUST NOT grow larger than 16MB, or chaos will
        # ensue.  We thus limit the slot history size to 4MB, to keep suffient
        # space for the actual operational data
        if  len(str(self._slot_history)) < 4 * 1024 * 1024 :
            self._slot_history.append (self._slot_status (short=True))
        else :
            # just replace the last entry with the current one.
            self._slot_history[-1]  =  self._slot_status (short=True)


    # ------------------------------------------------------------------------
    #
    def _launch_task(self, task, launch_method, launch_command):

        # create working directory in case it
        # doesn't exist
        try :
            os.makedirs(task.workdir)
        except OSError as e :
            # ignore failure on existing directory
            if  e.errno == errno.EEXIST and os.path.isdir (task.workdir) :
                pass
            else :
                raise

        # Start a new subprocess to launch the task
        proc = _Process(
            task=task,
            all_slots=self._slots,
            cores_per_node=self._cores_per_node,
            launch_method=launch_method,
            launch_command=launch_command,
            logger=self._log)

        task.started=datetime.datetime.utcnow()
        task.state = EXECUTING
        task._proc = proc

        # Add to the list of monitored tasks
        self._running_tasks.append(task) # add task here?

        # Update to mongodb
        #
        # AM: FIXME: this mongodb update is effectively a (or rather multiple)
        # synchronous remote operation(s) in the exec worker main loop.  Even if
        # spanning multiple exec workers, we would still share the mongodb
        # channel, which would still need serialization.  This is rather
        # inefficient.  We should consider to use a async communication scheme.
        # For example, we could collect all messages for a second (but not
        # longer) and send those updates in a bulk.
        self._update_tasks(task)


    # ------------------------------------------------------------------------
    # Iterate over all running tasks, check their status, and decide on the next step.
    # Also check for a requested cancellation for the task.
    def _check_running(self):

        idle = True

        # we update tasks in 'bulk' after each iteration.
        # all tasks that require DB updates are in update_tasks
        update_tasks = []

        # We record all completed tasks
        finished_tasks = []

        for task in self._running_tasks:

            # Get the subprocess object to poll on
            proc = task._proc
            ret_code = proc.poll()
            if ret_code is None:
                # Process is still running

                if task.uid in self._cuids_to_cancel:
                    # We got a request to cancel this task.
                    proc.kill()
                    state = CANCELED
                    finished_tasks.append(task)
                else:
                    # No need to continue [sic] further for this iteration
                    continue
            else:
                # The task ended (eventually FAILED or DONE).
                finished_tasks.append(task)

                # Make sure all stuff reached the spindles
                proc.close_and_flush_filehandles()

                # Convenience shortcut
                uid = task.uid
                self._log.info("Task %s terminated with return code %s." % (uid, ret_code))

                if ret_code != 0:
                    # The task failed, no need to deal with its output data.
                    state = FAILED
                else:
                    # The task finished cleanly, see if we need to deal with output data.

                    if task.agent_output_staging or task.ftw_output_staging:

                        state = STAGING_OUTPUT # TODO: this should ideally be PendingOutputStaging,
                                               # but that introduces a race condition currently

                        # Check if there are Directives that need to be performed by the Agent.
                        if task.agent_output_staging:

                            # Find the task in the database
                            # TODO: shouldnt this be available somewhere already, that would save a roundtrip?!
                            cu = self._cu.find_one({"_id": ObjectId(uid)})

                            for directive in cu['Agent_Output_Directives']:
                                output_staging = {
                                    'directive': directive,
                                    'sandbox': task.workdir,
                                    # TODO: the staging/area pilot directory should  not be derived like this:
                                    'staging_area': os.path.join(os.path.dirname(task.workdir), STAGING_AREA),
                                    'cu_id': uid
                                }

                                # Put the output staging directives in the queue
                                self._output_staging_queue.put(output_staging)

                                self._cu.update(
                                    {"_id": ObjectId(uid)},
                                    {"$set": {"Agent_Output_Status": EXECUTING}}
                                )

                        # Check if there are Directives that need to be performed
                        # by the FTW.
                        # Obviously these are not executed here (by the Agent),
                        # but we need this code to set the state so that the FTW
                        # gets notified that it can start its work.
                        if task.ftw_output_staging:
                            self._cu.update(
                                {"_id": ObjectId(uid)},
                                {"$set": {"FTW_Output_Status": PENDING}}
                            )
                    else:
                        # If there is no output data to deal with, the task becomes DONE
                        state = DONE

            #
            # At this stage the task is ended: DONE, FAILED or CANCELED.
            #

            idle = False

            # store stdout and stderr to the database
            workdir = task.workdir
            task_id = task.uid

            if  os.path.isfile(task.stdout_file):
                with open(task.stdout_file, 'r') as stdout_f:
                    txt = unicode(stdout_f.read(), "utf-8")

                    if  len(txt) > MAX_IO_LOGLENGTH :
                        txt = "[... CONTENT SHORTENED ...]\n%s" % txt[-MAX_IO_LOGLENGTH:]
                    task.stdout += txt

            if  os.path.isfile(task.stderr_file):
                with open(task.stderr_file, 'r') as stderr_f:
                    txt = unicode(stderr_f.read(), "utf-8")

                    if  len(txt) > MAX_IO_LOGLENGTH :
                        txt = "[... CONTENT SHORTENED ...]\n%s" % txt[-MAX_IO_LOGLENGTH:]
                    task.stderr += txt

            task.exit_code = ret_code

            # Record the time and state
            task.finished = datetime.datetime.utcnow()
            task.state = state

            # Put it on the list of tasks to update in bulk
            update_tasks.append(task)

            # Free the Slots, Flee the Flots, Ree the Frots!
            self._change_slot_states(task.slots, FREE)

        #
        # At this stage we are outside the for loop of running tasks.
        #

        # Update all the tasks that were marked for update.
        self._update_tasks(update_tasks)

        # Remove all tasks that don't require monitoring anymore.
        for e in finished_tasks:
            self._running_tasks.remove(e)

        return idle

    # ------------------------------------------------------------------------
    #
    def _update_tasks(self, tasks):
        """Updates the database entries for one or more tasks, including
        task state, log, etc.
        """

        if  not isinstance(tasks, list):
            tasks = [tasks]

        ts = datetime.datetime.utcnow()
        # We need to know which unit manager we are working with. We can pull
        # this information here:

        # Update capabilities
        #self._capability = self._slots2caps(self._slots)
        self._capability = self._slots2free(self._slots)

        # AM: FIXME: this at the moment pushes slot history whenever a task
        # state is updated...  This needs only to be done on ExecWorker
        # shutdown.  Well, alas, there is currently no way for it to find out
        # when it is shut down... Some quick and  superficial measurements 
        # though show no negative impact on agent performance.
        # AM: the capability publication cannot be delayed until shutdown
        # though...
        if  self._benchmark :
            if  self._slot_history_old != self._slot_history or \
                self._capability_old   != self._capability   :

                self._p.update(
                    {"_id": ObjectId(self._pilot_id)},
                    {"$set": {"slothistory" : self._slot_history,
                              #"slots"       : self._slots,
                              "capability"  : self._capability
                             }
                    }
                    )

                self._slot_history_old = self._slot_history[:]
                self._capability_old   = self._capability

        for task in tasks:
            self._cu.update({"_id": ObjectId(task.uid)}, 
            {"$set": {"state"         : task.state,
                      "started"       : task.started,
                      "finished"      : task.finished,
                      "slots"         : task.slots,
                      "exit_code"     : task.exit_code,
                      "stdout"        : task.stdout,
                      "stderr"        : task.stderr},
             "$push": {"statehistory": {"state": task.state, "timestamp": ts}}
            })

# ----------------------------------------------------------------------------
#
class InputStagingWorker(multiprocessing.Process):
    """An InputStagingWorker performs the agent side staging directives
       and writes the results back to MongoDB.
    """

    # ------------------------------------------------------------------------
    #
    def __init__(self, logger, staging_queue, mongodb_url, mongodb_name,
                 pilot_id, session_id):

        """ Creates a new InputStagingWorker instance.
        """
        multiprocessing.Process.__init__(self)
        self.daemon      = True
        self._terminate  = False

        self._log = logger

        self._unitmanager_id = None
        self._pilot_id = pilot_id

        mongo_client = pymongo.MongoClient(mongodb_url)
        self._mongo_db = mongo_client[mongodb_name]
        self._p  = mongo_db["%s.p"  % session_id]
        self._cu = mongo_db["%s.cu" % session_id]
        self._wm = mongo_db["%s.um" % session_id]

        self._staging_queue = staging_queue



    # ------------------------------------------------------------------------
    #
    def stop(self):
        """Terminates the process' main loop.
        """
        self._terminate = True

    # ------------------------------------------------------------------------
    #
    def run(self):

        self._log.info('InputStagingWorker started ...')

        while self._terminate is False:
            try:
                staging = self._staging_queue.get_nowait()
            except Queue.Empty:
                # do nothing and sleep if we don't have any queued staging
                time.sleep(0.1)
                continue

            # Perform input staging
            directive = staging['directive']
            if isinstance(directive, tuple):
                self._log.warning('Directive is a tuple %s and %s' % (directive, directive[0]))
                directive = directive[0] # TODO: Why is it a fscking tuple?!?!

            sandbox = staging['sandbox']
            staging_area = staging['staging_area']
            cu_id = staging['cu_id']
            self._log.info('Task input staging directives %s for cu: %s to %s' % (directive, cu_id, sandbox))

            # Create working directory in case it doesn't exist yet
            try :
                os.makedirs(sandbox)
            except OSError as e:
                # ignore failure on existing directory
                if e.errno == errno.EEXIST and os.path.isdir(sandbox):
                    pass
                else:
                    raise

            # Convert the source_url into a SAGA Url object
            source_url = saga.Url(directive['source'])

            if source_url.scheme == 'staging':
                self._log.info('Operating from staging')
                # Remove the leading slash to get a relative path from the staging area
                rel2staging = source_url.path.split('/',1)[1]
                source = os.path.join(staging_area, rel2staging)
            else:
                self._log.info('Operating from absolute path')
                source = source_url.path

            # Get the target from the directive and convert it to the location in the sandbox
            target = directive['target']
            abs_target = os.path.join(sandbox, target)

            log_message = ''
            try:
                # Act upon the directive now.

                if directive['action'] == LINK:
                    log_message = 'Linking %s to %s' % (source, abs_target)
                    os.symlink(source, abs_target)
                elif directive['action'] == COPY:
                    log_message = 'Copying %s to %s' % (source, abs_target)
                    shutil.copyfile(source, abs_target)
                elif directive['action'] == MOVE:
                    log_message = 'Moving %s to %s' % (source, abs_target)
                    shutil.move(source, abs_target)
                elif directive['action'] == TRANSFER:
                    # TODO: SAGA REMOTE TRANSFER
                    log_message = 'Transferring %s to %s' % (source, abs_target)
                else:
                    raise Exception('Action %s not supported' % directive['action'])

                # If we reached this far, assume the staging succeeded
                log_message += ' succeeded.'
                self._log.info(log_message)

                # If all went fine, update the state of this StagingDirective to Done
                self._cu.update({'_id': ObjectId(cu_id),
                                 'Agent_Input_Status': EXECUTING,
                                 'Agent_Input_Directives.state': PENDING,
                                 'Agent_Input_Directives.source': directive['source'],
                                 'Agent_Input_Directives.target': directive['target']},
                                {'$set' : {'Agent_Input_Directives.$.state': DONE},
                                 '$push': {'log': log_message}})

            except:
                # If we catch an exception, assume the staging failed
                log_message += ' failed.'
                self._log.error(log_message)

                # If a staging directive fails, fail the CU also.
                self._cu.update({'_id': ObjectId(cu_id),
                                 'Agent_Input_Status': EXECUTING,
                                 'Agent_Input_Directives.state': PENDING,
                                 'Agent_Input_Directives.source': directive['source'],
                                 'Agent_Input_Directives.target': directive['target']},
                                {'$set': {'Agent_Input_Directives.$.state': FAILED,
                                          'Agent_Input_Status': FAILED,
                                          'state': FAILED},
                                 '$push': {'log': 'Marking Compute Unit FAILED because of FAILED Staging Directive.'}})


# ----------------------------------------------------------------------------
#
class OutputStagingWorker(multiprocessing.Process):
    """An OutputStagingWorker performs the agent side staging directives
       and writes the results back to MongoDB.
    """

    # ------------------------------------------------------------------------
    #
    def __init__(self, logger, staging_queue, mongodb_url, mongodb_name,
                 pilot_id, session_id):

        """ Creates a new OutputStagingWorker instance.
        """
        multiprocessing.Process.__init__(self)
        self.daemon      = True
        self._terminate  = False

        self._log = logger

        self._unitmanager_id = None
        self._pilot_id = pilot_id

        mongo_client = pymongo.MongoClient(mongodb_url)
        self._mongo_db = mongo_client[mongodb_name]
        self._p  = mongo_db["%s.p"  % session_id]
        self._cu = mongo_db["%s.cu" % session_id]
        self._wm = mongo_db["%s.um" % session_id]

        self._staging_queue = staging_queue



    # ------------------------------------------------------------------------
    #
    def stop(self):
        """Terminates the process' main loop.
        """
        self._terminate = True

    # ------------------------------------------------------------------------
    #
    def run(self):

        self._log.info('OutputStagingWorker started ...')

        try:
            while self._terminate is False:
                try:
                    staging = self._staging_queue.get_nowait()

                    # Perform output staging
                    directive = staging['directive']
                    if isinstance(directive, tuple):
                        self._log.warning('Directive is a tuple %s and %s' % (directive, directive[0]))
                        directive = directive[0] # TODO: Why is it a fscking tuple?!?!

                    sandbox = staging['sandbox']
                    staging_area = staging['staging_area']
                    cu_id = staging['cu_id']
                    self._log.info('Task output staging directives %s for cu: %s to %s' % (directive, cu_id, sandbox))

                    source = str(directive['source'])
                    abs_source = os.path.join(sandbox, source)

                    # Convert the target_url into a SAGA Url object
                    target_url = saga.Url(directive['target'])

                    # Handle special 'staging' scheme
                    if target_url.scheme == 'staging':
                        self._log.info('Operating from staging')
                        # Remove the leading slash to get a relative path from the staging area
                        rel2staging = target_url.path.split('/',1)[1]
                        target = os.path.join(staging_area, rel2staging)
                    else:
                        self._log.info('Operating from absolute path')
                        target = target_url.path

                    # Create output directory in case it doesn't exist yet
                    try :
                        os.makedirs(os.path.dirname(target))
                    except OSError as e:
                        # ignore failure on existing directory
                        if e.errno == errno.EEXIST and os.path.isdir(os.path.dirname(target)):
                            pass
                        else:
                            raise

                    if directive['action'] == LINK:
                        self._log.info('Going to link %s to %s' % (abs_source, target))
                        os.symlink(abs_source, target)
                        logmessage = 'Linked %s to %s' % (abs_source, target)
                    elif directive['action'] == COPY:
                        self._log.info('Going to copy %s to %s' % (abs_source, target))
                        shutil.copyfile(abs_source, target)
                        logmessage = 'Copied %s to %s' % (abs_source, target)
                    elif directive['action'] == MOVE:
                        self._log.info('Going to move %s to %s' % (abs_source, target))
                        shutil.move(abs_source, target)
                        logmessage = 'Moved %s to %s' % (abs_source, target)
                    elif directive['action'] == TRANSFER:
                        self._log.info('Going to transfer %s to %s' % (directive['source'], os.path.join(sandbox, directive['target'])))
                        # TODO: SAGA REMOTE TRANSFER
                        logmessage = 'Transferred %s to %s' % (abs_source, target)
                    else:
                        # TODO: raise
                        self._log.error('Action %s not supported' % directive['action'])

                    # If all went fine, update the state of this StagingDirective to Done
                    self._cu.update({'_id' : ObjectId(cu_id),
                                     'Agent_Output_Status': EXECUTING,
                                     'Agent_Output_Directives.state': PENDING,
                                     'Agent_Output_Directives.source': directive['source'],
                                     'Agent_Output_Directives.target': directive['target']},
                                    {'$set' : {'Agent_Output_Directives.$.state': DONE},
                                     '$push': {'log': logmessage}})

                except Queue.Empty:
                    # do nothing and sleep if we don't have any queued staging
                    time.sleep(0.1)


        except Exception, ex:
            self._log.error("Error in OutputStagingWorker loop: %s", traceback.format_exc())
            raise


# ----------------------------------------------------------------------------
#
class Agent(threading.Thread):

    # ------------------------------------------------------------------------
    #
    def __init__(self, logger, exec_env, runtime, mongodb_url, mongodb_name, mongodb_auth, 
                 pilot_id, session_id, benchmark):
        """Le Constructeur creates a new Agent instance.
        """
        threading.Thread.__init__(self)
        self.daemon      = True
        self.lock        = threading.Lock()
        self._terminate  = threading.Event()

        self._log        = logger
        self._pilot_id   = pilot_id
        self._exec_env   = exec_env
        self._runtime    = runtime
        self._starttime  = None
        self._benchmark  = benchmark

        self._workdir    = os.getcwd()

        mongo_client = pymongo.MongoClient(mongodb_url)
        mongo_db = mongo_client[mongodb_name]

        # do auth on username *and* password (ignore empty split results)
        auth_elems = filter (None, mongodb_auth.split (':', 1))
        if  len (auth_elems) == 2 :
            mongo_db.authenticate (auth_elems[0], auth_elems[1])

        self._p  = mongo_db["%s.p"  % session_id]
        self._cu = mongo_db["%s.cu" % session_id]
        self._wm = mongo_db["%s.um" % session_id]

        # the task queue holds the tasks that are pulled from the MongoDB
        # server. The ExecWorkers compete for the tasks in the queue. 
        self._task_queue = multiprocessing.Queue()

        # The staging queues holds the staging directives to be performed
        self._input_staging_queue = multiprocessing.Queue()
        self._output_staging_queue = multiprocessing.Queue()

        # Channel for the Agent to communicate commands with the ExecWorker
        self._command_queue = multiprocessing.Queue()

        # we assign each node partition to a task execution worker
        self._exec_worker = ExecWorker(
            logger          = self._log,
            task_queue      = self._task_queue,
            output_staging_queue   = self._output_staging_queue,
            command_queue   = self._command_queue,
            node_list       = self._exec_env.lrms.node_list,
            cores_per_node  = self._exec_env.lrms.cores_per_node,
            launch_methods  = self._exec_env.discovered_launch_methods,
            mongodb_url     = mongodb_url,
            mongodb_name    = mongodb_name,
            mongodb_auth    = mongodb_auth,
            pilot_id        = pilot_id,
            session_id      = session_id,
            benchmark       = benchmark
        )
        self._exec_worker.start()
        self._log.info("Started up %s serving nodes %s" % (self._exec_worker, self._exec_env.node_list))

        # Start input staging worker
        input_staging_worker = InputStagingWorker(
            logger          = self._log,
            staging_queue   = self._input_staging_queue,
            mongodb_url     = mongodb_url,
            mongodb_name    = mongodb_name,
            pilot_id        = pilot_id,
            session_id      = session_id
        )
        input_staging_worker.start()
        self._log.info("Started up %s." % input_staging_worker)
        self._input_staging_worker = input_staging_worker

        # Start output staging worker
        output_staging_worker = OutputStagingWorker(
            logger          = self._log,
            staging_queue   = self._output_staging_queue,
            mongodb_url     = mongodb_url,
            mongodb_name    = mongodb_name,
            pilot_id        = pilot_id,
            session_id      = session_id
        )
        output_staging_worker.start()
        self._log.info("Started up %s." % output_staging_worker)
        self._output_staging_worker = output_staging_worker

    # ------------------------------------------------------------------------
    #
    def stop(self):
        """Terminate the agent main loop.
        """
        # First, we need to shut down all the workers
        self._exec_worker.terminate()

        # Shut down the staging workers
        self._input_staging_worker.terminate()
        self._output_staging_worker.terminate()

        # Next, we set our own termination signal
        self._terminate.set()

    # ------------------------------------------------------------------------
    #
    def run(self):
        """Starts the thread when Thread.start() is called.
        """
        # first order of business: set the start time and state of the pilot
        self._log.info("Agent %s starting ..." % self._pilot_id)
        ts = datetime.datetime.utcnow()
        ret = self._p.update(
            {"_id": ObjectId(self._pilot_id)}, 
            {"$set": {"state"          : ACTIVE,
                      "nodes"          : self._exec_env.node_list,
                      "cores_per_node" : self._exec_env.cores_per_node,
                      "started"        : ts,
                      "capability"     : 0},
             "$push": {"statehistory": {"state": ACTIVE, "timestamp": ts}}
            })
        # TODO: Check for return value, update should be true!
        self._log.info("Database updated! %s" % ret)

        self._starttime = time.time()

        while True:

            try:

                idle = True

                # Check the workers periodically. If they have died, we 
                # exit as well. this can happen, e.g., if the worker 
                # process has caught a ctrl+C
                if self._exec_worker.is_alive() is False:
                    pilot_FAILED(self._p, self._pilot_id, self._log, "Execution worker %s died." % str(self._exec_worker))
                    return

                # Exit the main loop if terminate is set. 
                if self._terminate.isSet():
                    pilot_CANCELED(self._p, self._pilot_id, self._log, "Terminated (_terminate set).")
                    return

                # Make sure that we haven't exceeded the agent runtime. if 
                # we have, terminate. 
                if time.time() >= self._starttime + (int(self._runtime) * 60):
                    self._log.info("Agent has reached runtime limit of %s seconds." % str(int(self._runtime)*60))
                    pilot_DONE(self._p, self._pilot_id)
                    return

                # Try to get new tasks from the database. for this, we check the 
                # cu_queue of the pilot. if there are new entries, we get them,
                # get the actual pilot entries for them and remove them from 
                # the cu_queue.
                try:

                    # Check if there's a command waiting
                    retdoc = self._p.find_and_modify(
                                query={"_id":ObjectId(self._pilot_id)},
                                update={"$set":{COMMAND_FIELD: []}}, # Wipe content of array
                                fields=[COMMAND_FIELD]
                    )

                    if retdoc:
                        commands = retdoc['commands']
                    else:
                        commands = []

                    for command in commands:

                        idle = False

                        if command[COMMAND_TYPE] == COMMAND_CANCEL_PILOT:
                            self._log.info("Received Cancel Pilot command.")
                            pilot_CANCELED(self._p, self._pilot_id, self._log, "CANCEL received. Terminating.")
                            return # terminate loop

                        elif command[COMMAND_TYPE] == COMMAND_CANCEL_COMPUTE_UNIT:
                            self._log.info("Received Cancel Compute Unit command for: %s" % command[COMMAND_ARG])
                            # Put it on the command queue of the ExecWorker
                            self._command_queue.put(command)

                        elif command[COMMAND_TYPE] == COMMAND_KEEP_ALIVE:
                            self._log.info("Received KeepAlive command.")
                        else:
                            raise Exception("Received unknown command: %s with arg: %s." % (command[COMMAND_TYPE], command[COMMAND_ARG]))

                    # Check if there are compute units waiting for execution,
                    # and log that we pulled it.
                    ts = datetime.datetime.utcnow()
                    cu_cursor = self._cu.find_and_modify(
                        query={"pilot" : self._pilot_id,
                               "state" : PENDING_EXECUTION},
                        update={"$set" : {"state": SCHEDULING},
                                "$push": {"statehistory": {"state": SCHEDULING, "timestamp": ts}}}
                    )

                    # There are new compute units in the cu_queue on the database.
                    # Get the corresponding cu entries.
                    if cu_cursor is not None:

                        idle = False

                        if not isinstance(cu_cursor, list):
                            cu_cursor = [cu_cursor]

                        for cu in cu_cursor:
                            # Create new task objects and put them into the task queue
                            w_uid = str(cu["_id"])
                            self._log.info("Found new tasks in pilot queue: %s" % w_uid)

                            task_dir_name = "%s/unit-%s" % (self._workdir, str(cu["_id"]))
                            stdout = cu["description"].get ('stdout')
                            stderr = cu["description"].get ('stderr')

                            if  stdout : stdout_file = task_dir_name+'/'+stdout
                            else       : stdout_file = task_dir_name+'/STDOUT'
                            if  stderr : stderr_file = task_dir_name+'/'+stderr
                            else       : stderr_file = task_dir_name+'/STDERR'

                            task = Task(uid         = w_uid,
                                        executable  = cu["description"]["executable"],
                                        arguments   = cu["description"]["arguments"],
                                        environment = cu["description"]["environment"],
                                        numcores    = cu["description"]["cores"],
                                        mpi         = cu["description"]["mpi"],
                                        pre_exec    = cu["description"]["pre_exec"],
                                        post_exec   = cu["description"]["post_exec"],
                                        workdir     = task_dir_name,
                                        stdout_file = stdout_file,
                                        stderr_file = stderr_file,
                                        agent_output_staging = True if cu['Agent_Output_Directives'] else False,
                                        ftw_output_staging   = True if cu['FTW_Output_Directives'] else False
                                        )

                            task.state = SCHEDULING
                            self._task_queue.put(task)

                    #
                    # Check if there are compute units waiting for input staging
                    #
                    ts = datetime.datetime.utcnow()
                    cu_cursor = self._cu.find_and_modify(
                        query={'pilot' : self._pilot_id,
                               'Agent_Input_Status': PENDING},
                        # TODO: This might/will create double state history for StagingInput
                        update={'$set' : {'Agent_Input_Status': EXECUTING,
                                          'state': STAGING_INPUT},
                                '$push': {'statehistory': {'state': STAGING_INPUT, 'timestamp': ts}}}#,
                        #limit=BULK_LIMIT
                    )
                    if cu_cursor is not None:

                        idle = False

                        if not isinstance(cu_cursor, list):
                            cu_cursor = [cu_cursor]

                        for cu in cu_cursor:
                            for directive in cu['Agent_Input_Directives']:
                                input_staging = {
                                    'directive': directive,
                                    'sandbox': os.path.join(self._workdir,
                                                            'unit-%s' % str(cu['_id'])),
                                    'staging_area': os.path.join(self._workdir, 'staging_area'),
                                    'cu_id': str(cu['_id'])
                                }

                                # Put the input staging directives in the queue
                                self._input_staging_queue.put(input_staging)

                except Exception, ex:
                    raise

                if  idle :
                    time.sleep(1)

            except Exception, ex:
                # If we arrive here, there was an exception in the main loop.
                pilot_FAILED(self._p, self._pilot_id, self._log, 
                    "ERROR in agent main loop: %s. %s" % (str(ex), traceback.format_exc()))
                return

        # MAIN LOOP TERMINATED
        return

#-----------------------------------------------------------------------------
#
class _Process(subprocess.Popen):

    #-------------------------------------------------------------------------
    #
    def __init__(self, task, all_slots, cores_per_node, launch_method,
                 launch_command, logger):

        self._task = task
        self._log  = logger

        launch_script = tempfile.NamedTemporaryFile(prefix='radical_pilot_cu_launch_script-', dir=task.workdir, suffix=".sh", delete=False)
        self._log.debug('Created launch_script: %s' % launch_script.name)
        st = os.stat(launch_script.name)
        os.chmod(launch_script.name, st.st_mode | stat.S_IEXEC)
        launch_script.write('#!/bin/bash -l\n')
        launch_script.write('cd %s\n' % task.workdir)

        # Before the Big Bang there was nothing
        pre_exec = task.pre_exec
        pre_exec_string = ''
        if pre_exec:
            if not isinstance(pre_exec, list):
                pre_exec = [pre_exec]
            for bb in pre_exec:
                pre_exec_string += "%s\n" % bb

        # After the universe dies the infrared death, there will be nothing
        post_exec = task.post_exec
        post_exec_string = ''
        if post_exec:
            if not isinstance(post_exec, list):
                post_exec = [post_exec]
            for bb in post_exec:
                post_exec_string += "%s\n" % bb

        # executable and arguments
        if task.executable is not None:
            task_exec_string = task.executable # TODO: Do we allow $ENV/bin/program constructs here?
        else:
            raise Exception("No executable specified!") # TODO: This should be catched earlier problaby

        task_args_string = ''
        if task.arguments is not None:
            for arg in task.arguments:

                if  not arg :
                    continue # ignore empty args

                arg = arg.replace ('"', '\\"') # Escape all double quotes
                if  arg[0] == arg[-1] == "'" : # If a string is between outer single quotes,
                    task_args_string += '%s ' % arg # ... pass it as is.
                else :
                    task_args_string += '"%s" ' % arg # Otherwise return between double quotes.

        # Create string for environment variable setting
        env_string = ''
        if task.environment is not None and len(task.environment.keys()):
            env_string += 'export'
            for key in task.environment:
                env_string += ' %s=%s' % (key, task.environment[key])


        # Based on the launch method we use different, well, launch methods
        # to launch the task. just on the shell, via mpirun, ssh, ibrun or aprun
        if launch_method == LAUNCH_METHOD_LOCAL:
            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s\n'    % task_exec_string)
            launch_script.write('%s\n'    % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_MPIRUN:
            # Construct the hosts_string
            hosts_string = ''
            for slot in task.slots:
                host = slot.split(':')[0]
                hosts_string += '%s,' % host

            mpirun_command = "%s -np %s -host %s" % (launch_command,
                                                     task.numcores, hosts_string)

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s %s\n' % (mpirun_command, task_exec_string))
            launch_script.write('%s\n'    % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_MPIRUN_RSH:
            # Construct the hosts_string
            hosts_string = ''
            for slot in task.slots:
                host = slot.split(':')[0]
                hosts_string += ' %s' % host

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            mpirun_rsh_command = "%s -export -np %s%s" % (launch_command, task.numcores, hosts_string)

            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s %s\n' % (mpirun_rsh_command, task_exec_string))
            launch_script.write('%s\n'    % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_MPIEXEC:
            # Construct the hosts_string
            hosts_string = ''
            for slot in task.slots:
                host = slot.split(':')[0]
                hosts_string += '%s,' % host

            mpiexec_command = "%s -n %s -hosts %s" % (launch_command, task.numcores, hosts_string)

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s %s\n' % (mpiexec_command, task_exec_string))
            launch_script.write('%s\n'    % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_APRUN:

            aprun_command = "%s -n %s" % (launch_command, task.numcores)

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s %s\n' % (aprun_command, task_exec_string))
            launch_script.write('%s\n'    % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_RUNJOB:

            if task.numcores % 16:
                msg = "Num cores (%d) is not a multiple of 16!" % task.numcores
                self._log.error(msg)
                raise Exception(msg)

            loadl_job_name = os.environ.get('LOADL_JOB_NAME')
            if loadl_job_name is None:
                msg = "$LOADL_JOB_NAME not set!"
                self._log.error(msg)
                raise Exception(msg)

            # Get the board list and block shape from 'llq -l' output
            output = subprocess.check_output(["llq", "-l", loadl_job_name])
            loadl_bg_board_list_str = None
            loadl_bg_block_shape_str = None
            for line in output.splitlines():
                # Detect BG board list
                if "BG Node Board List: " in line:
                    loadl_bg_board_list_str = line.split(':')[1].strip()
                elif "BG Shape Allocated: " in line:
                    loadl_bg_block_shape_str = line.split(':')[1].strip()
            if not loadl_bg_board_list_str:
                msg = "No board list found in llq output!"
                self._log.error(msg)
                raise Exception(msg)
            if not loadl_bg_block_shape_str:
                msg = "No board shape found in llq output!"
                self._log.error(msg)
                raise Exception(msg)

            # Build nodes data structure
            loadl_node_list = bgq_shapeandboards2block(loadl_bg_block_shape_str, loadl_bg_board_list_str)

            first_slot = task.slots[0]
            # Get the host and the core part
            [first_slot_host, first_slot_core] = first_slot.split(':')
            # Find the entry in the the all_slots list based on the host,
            # use a generator to not have to go through all of the list.
            slot_entry = (slot for slot in all_slots if slot["node"] == first_slot_host).next()
            # Transform it into an index in to the all_slots list
            all_slots_slot_index = all_slots.index(slot_entry)

            # Construct sub-block table
            # TODO: this needs to be done once at a better place
            shape_table = bgq_create_sub_block_shape_table(loadl_bg_block_shape_str)

            # Runjob it is!
            runjob_command = launch_command

            # Run this subjob in the block communicated by LoadLeveler
            # TODO: Don't use env var, but the value we extracted earlier!
            runjob_command += ' --block $LOADL_BG_BLOCK'

            # Determine the offset of the starting node in the node list
            # and use that as the corner for the subjob.
            #runjob_offset = all_slots_slot_index * cores_per_node + int(first_slot_core)
            runjob_offset = all_slots_slot_index
            corner_node = loadl_node_list[runjob_offset][BGQ_BLOCK_NAME]
            runjob_command += ' --corner %s' % corner_node

            # Determine the shape based on the number of nodes we require
            shape = shape_table[task.numcores/BGQ_CORES_PER_NODE]
            runjob_command += ' --shape %s' % bgq_shape2str(shape)

            # And finally add the executable and the arguments
            runjob_command += ' --exe %s' % task_exec_string
            if task_args_string:
                runjob_command += ' --args %s' % task_args_string

            launch_script.write('%s\n' % pre_exec_string)
            launch_script.write('%s\n' % env_string)
            launch_script.write('%s\n' % runjob_command)
            launch_script.write('%s\n' % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_IBRUN:
            # NOTE: Don't think that with IBRUN it is possible to have
            # processes != cores ...

            first_slot = task.slots[0]
            # Get the host and the core part
            [first_slot_host, first_slot_core] = first_slot.split(':')
            # Find the entry in the the all_slots list based on the host
            slot_entry = (slot for slot in all_slots if slot["node"] == first_slot_host).next()
            # Transform it into an index in to the all_slots list
            all_slots_slot_index = all_slots.index(slot_entry)

            # TODO: This assumes all hosts have the same number of cores
            ibrun_offset = all_slots_slot_index * cores_per_node + int(first_slot_core)
            ibrun_command = "%s -n %s -o %d" % \
                            (launch_command, task.numcores,
                             ibrun_offset)

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            # Build launch script
            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s %s\n' % (ibrun_command, task_exec_string))
            launch_script.write('%s\n'    % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_DPLACE:
            # Use dplace on SGI

            first_slot = task.slots[0]
            # Get the host and the core part
            [first_slot_host, first_slot_core] = first_slot.split(':')
            # Find the entry in the the all_slots list based on the host
            slot_entry = (slot for slot in all_slots if slot["node"] == first_slot_host).next()
            # Transform it into an index in to the all_slots list
            all_slots_slot_index = all_slots.index(slot_entry)

            dplace_offset = all_slots_slot_index * cores_per_node + int(first_slot_core)
            dplace_command = "%s -c %d-%d" % \
                                    (launch_command, dplace_offset, dplace_offset+task.numcores-1)

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            # Build launch script
            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s %s\n' % (dplace_command, task_exec_string))
            launch_script.write('%s\n'    % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_MPIRUN_DPLACE:
            # Use mpirun in combination with dplace

            first_slot = task.slots[0]
            # Get the host and the core part
            [first_slot_host, first_slot_core] = first_slot.split(':')
            # Find the entry in the the all_slots list based on the host
            slot_entry = (slot for slot in all_slots if slot["node"] == first_slot_host).next()
            # Transform it into an index in to the all_slots list
            all_slots_slot_index = all_slots.index(slot_entry)

            # TODO: this should be passed on from the detection mechanism
            dplace_command = 'dplace'

            dplace_offset = all_slots_slot_index * cores_per_node + int(first_slot_core)
            mpirun_dplace_command = "%s -np %d %s -c %d-%d" % \
                            (launch_command, task.numcores, dplace_command, dplace_offset, dplace_offset+task.numcores-1)

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            # Build launch script
            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s %s\n' % (mpirun_dplace_command, task_exec_string))
            launch_script.write('%s\n'    % post_exec_string)

            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_POE:

            # Count slots per host in provided slots description.
            hosts = {}
            for slot in task.slots:
                host = slot.split(':')[0]
                if host not in hosts:
                    hosts[host] = 1
                else:
                    hosts[host] += 1

            # Create string with format: "hostX N host
            hosts_string = ''
            for host in hosts:
                hosts_string += '%s %d ' % (host, hosts[host])

            # Override the LSB_MCPU_HOSTS env variable as this is set by
            # default to the size of the whole pilot.
            poe_command = 'LSB_MCPU_HOSTS="%s" %s' % (
                hosts_string, launch_command)

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            # Continue to build launch script
            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s %s\n' % (poe_command, task_exec_string))
            launch_script.write('%s\n'    % post_exec_string)

            # Command line to execute launch script
            cmdline = launch_script.name

        elif launch_method == LAUNCH_METHOD_SSH:
            host = task.slots[0].split(':')[0] # Get the host of the first entry in the acquired slot

            if task_args_string:
                task_exec_string += ' %s' % task_args_string

            # Continue to build launch script
            launch_script.write('%s\n'    % pre_exec_string)
            launch_script.write('%s\n'    % env_string)
            launch_script.write('%s\n'    % task_exec_string)
            launch_script.write('%s\n'    % post_exec_string)

            # Command line to execute launch script
            cmdline = '%s %s %s' % (launch_command, host, launch_script.name)

        else:
            raise NotImplementedError("Launch method %s not implemented in executor!" % launch_method)

        # We are done writing to the launch script, its ready for execution now.
        launch_script.close()

        self._stdout_file_h = open(task.stdout_file, "w")
        self._stderr_file_h = open(task.stderr_file, "w")

        self._log.info("Launching task %s via %s in %s" % (task.uid, cmdline, task.workdir))

        super(_Process, self).__init__(args=cmdline,
                                       bufsize=0,
                                       executable=None,
                                       stdin=None,
                                       stdout=self._stdout_file_h,
                                       stderr=self._stderr_file_h,
                                       preexec_fn=None,
                                       close_fds=True,
                                       shell=True,
                                       cwd=task.workdir, # TODO: This doesn't always make sense if it runs remotely
                                       env=None,
                                       universal_newlines=False,
                                       startupinfo=None,
                                       creationflags=0)

    #-------------------------------------------------------------------------
    #
    @property
    def task(self):
        """Returns the task object associated with the process.
        """
        return self._task

    #-------------------------------------------------------------------------
    #
    def close_and_flush_filehandles(self):
        self._stdout_file_h.flush()
        self._stderr_file_h.flush()
        self._stdout_file_h.close()
        self._stderr_file_h.close()


#-----------------------------------------------------------------------------
#
def parse_commandline():

    parser = optparse.OptionParser()

    parser.add_option('-a', '--mongodb-auth',
                      metavar='AUTH',
                      dest='mongodb_auth',
                      help='username:password for MongoDB access.')

    parser.add_option('-b', '--benchmark',
                      metavar='BENCHMARK',
                      type='int',
                      dest='benchmark',
                      help='Enables timing for benchmarking purposes.')

    parser.add_option('-c', '--cores',
                      metavar='CORES',
                      dest='cores',
                      type='int',
                      help='Specifies the number of cores to allocate.')

    parser.add_option('-d', '--debug',
                      metavar='DEBUG',
                      dest='debug_level',
                      type='int',
                      help='The DEBUG level for the agent.')

    parser.add_option('-j', '--task-launch-method',
                      metavar='METHOD',
                      dest='task_launch_method',
                      help='Specifies the task launch method.')

    parser.add_option('-k', '--mpi-launch-method',
                      metavar='METHOD',
                      dest='mpi_launch_method',
                      help='Specifies the MPI launch method.')

    parser.add_option('-l', '--lrms',
                      metavar='LRMS',
                      dest='lrms',
                      help='Specifies the LRMS type.')

    parser.add_option('-m', '--mongodb-url',
                      metavar='URL',
                      dest='mongodb_url',
                      help='Specifies the MongoDB Url.')

    parser.add_option('-n', '--database-name',
                      metavar='URL',
                      dest='database_name',
                      help='Specifies the MongoDB database name.')

    parser.add_option('-p', '--pilot-id',
                      metavar='PID',
                      dest='pilot_id',
                      help='Specifies the Pilot ID.')

    parser.add_option('-s', '--session-id',
                      metavar='SID',
                      dest='session_id',
                      help='Specifies the Session ID.')

    parser.add_option('-t', '--runtime',
                      metavar='RUNTIME',
                      dest='runtime',
                      help='Specifies the agent runtime in minutes.')

    parser.add_option('-v', '--version',
                      metavar='VERSION ',
                      dest='package_version',
                      help='The RADICAL-Pilot package version.')

    # parse the whole shebang
    (options, args) = parser.parse_args()

    if options.mongodb_url is None:
        parser.error("You must define MongoDB URL (-m/--mongodb-url). Try --help for help.")
    if options.database_name is None:
        parser.error("You must define a database name (-n/--database-name). Try --help for help.")
    if options.session_id is None:
        parser.error("You must define a session id (-s/--session-id). Try --help for help.")
    if options.pilot_id is None:
        parser.error("You must define a pilot id (-p/--pilot-id). Try --help for help.")
    if options.cores is None:
        parser.error("You must define the number of cores (-c/--cores). Try --help for help.")
    if options.runtime is None:
        parser.error("You must define the agent runtime (-t/--runtime). Try --help for help.")
    if options.package_version is None:
        parser.error("You must pass the RADICAL-Pilot package version (-v/--version). Try --help for help.")
    if options.debug_level is None:
        parser.error("You must pass the DEBUG level (-d/--debug). Try --help for help.")
    if options.lrms is None:
        parser.error("You must pass the LRMS (-l/--lrms). Try --help for help.")

    return options

#-----------------------------------------------------------------------------
#
if __name__ == "__main__":

    # parse command line options
    options = parse_commandline()

    # configure the agent logger
    logger = logging.getLogger('radical.pilot.agent')
    logger.setLevel(options.debug_level)
    ch = logging.FileHandler("AGENT.LOG")
    #ch.setLevel(logging.DEBUG) # TODO: redundant if you have just one file?
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    ch.setFormatter(formatter)
    logger.addHandler(ch)
    logger.info("RADICAL-Pilot multi-core agent for package/API version %s" % options.package_version)

    logger.info("Using SAGA version %s" % saga.version)

    #--------------------------------------------------------------------------
    # Establish database connection
    try:
        host, port = options.mongodb_url.split(':', 1)
        mongo_client = pymongo.MongoClient(options.mongodb_url)
        mongo_db     = mongo_client[options.database_name]

        if  len (options.mongodb_auth) >= 3 :
            user, pwd = options.mongodb_auth.split (':', 1)
            mongo_db.authenticate (user, pwd)

        mongo_p      = mongo_db["%s.p"  % options.session_id]
        mongo_cu      = mongo_db["%s.cu" % options.session_id]  # AM: never used
        mongo_wm     = mongo_db["%s.um" % options.session_id]  # AM: never used

    except Exception, ex:
        logger.error("Couldn't establish database connection: %s" % str(ex))
        sys.exit(1)

    #--------------------------------------------------------------------------
    # Some signal handling magic
    def sigint_handler(signal, frame):
        msg = 'Caught SIGINT. EXITING.'
        pilot_FAILED(mongo_p, options.pilot_id, logger, msg)
        sys.exit (1)
    signal.signal(signal.SIGINT, sigint_handler)

    def sigalarm_handler(signal, frame):
        msg = 'Caught SIGALRM (Walltime limit reached?). EXITING'
        pilot_FAILED(mongo_p, options.pilot_id, logger, msg)
        sys.exit (1)
    signal.signal(signal.SIGALRM, sigalarm_handler)

    #--------------------------------------------------------------------------
    # Discover environment, nodes, cores, mpi, etc.
    try:
        exec_env = ExecutionEnvironment(
            logger=logger,
            lrms_name=options.lrms,
            requested_cores=options.cores,
            task_launch_method=options.task_launch_method,
            mpi_launch_method=options.mpi_launch_method
        )
        if exec_env is None:
            msg = "Couldn't set up execution environment."
            logger.error(msg)
            pilot_FAILED(mongo_p, options.pilot_id, logger, msg)
            sys.exit (1)

    except Exception, ex:
        msg = "Error setting up execution environment: %s" % str(ex)
        logger.error(msg)
        pilot_FAILED(mongo_p, options.pilot_id, logger, msg)
        sys.exit (1)

    #--------------------------------------------------------------------------
    # Launch the agent thread
    agent = None
    try:
        agent = Agent(logger=logger,
                      exec_env=exec_env,
                      runtime=options.runtime,
                      mongodb_url=options.mongodb_url,
                      mongodb_name=options.database_name,
                      mongodb_auth=options.mongodb_auth,
                      pilot_id=options.pilot_id,
                      session_id=options.session_id, 
                      benchmark=options.benchmark)

        # AM: why is this done in a thread?  This thread blocks anyway, so it
        # could just *do* the things.  That would avoid those global vars and
        # would allow for cleaner shutdown.
        agent.start()
        agent.join()

    except Exception as ex:
        msg = "Error running agent: %s" % str(ex)
        print traceback.format_exc()
        logger.error(msg)
        pilot_FAILED(mongo_p, options.pilot_id, logger, msg)
        if  agent :
            agent.stop()
        sys.exit (1)

    except SystemExit:

        logger.error("Caught keyboard interrupt. EXITING")
        if  agent :
            agent.stop()
