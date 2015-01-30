#!/usr/bin/env python

"""
.. module:: radical.pilot.agent
   :platform: Unix
   :synopsis: The agent for RADICAL-Pilot.

   The agent gets CUs by means of the MongoDB.
   The execution of CUs by the Agent is (primarily) configured by the
   triplet (LRMS, LAUNCH_METHOD(s), SCHEDULER):
   - The LRMS detects and structures the information about the resources
     available to agent.
   - The Scheduler maps the execution requests of the LaunchMethods to a
     subset of the resources available to the Agent.
     It does not deal with the "presentation" of this subset.
   - The LaunchMethods configure how to execute (regular and MPI) tasks,
     and know about the specific format to specify the subset of resources.


   Structure:
   ----------
   This represents the planned architecture, which is not fully represented in
   code, yet.

     - class Agent
       - represents the whole thing
       - has a set of StageinWorkers  (threads or procs)
       - has a set of StageoutWorkers (threads or procs)
       - has a set of ExecWorkers     (threads or procs)
       - has a set of UpdateWorkers   (threads or procs)
       - has a HeartbeatMonitor       (threads or procs)
       - has a inputstaging  queue
       - has a outputstaging queue
       - has a execution queue
       - has a update queue
       - loops forever
       - in each iteration
         - pulls CU bulks from DB
         - pushes CUs into inputstaging queue or execution queue (based on
           obvious metric)

     class StageinWorker
       - competes for CU input staging requests from inputstaging queue
       - for each received CU
         - performs staging
         - pushes CU into execution queue
         - pushes stage change notification request into update queue

     class StageoutWorker
       - competes for CU output staging requests from outputstaging queue
       - for each received CU
         - performs staging
         - pushes stage change notification request into update queue

     class ExecWorker
       - manages a partition of the allocated cores
         (partition size == max cu size)
       - competes for CU execution reqeusts from execute queue
       - for each CU
         - prepares execution command
         - pushes command to ExecutionEnvironment
         - pushes stage change notification request into update queue

     class Spawner
       - executes CUs according to ExecWorker instruction
       - monitors CU execution (for completion)
       - gets CU execution reqeusts from ExecWorker
       - for each CU
         - executes CU command
         - monitors CU execution
         - on CU completion
           - pushes CU to outputstaging queue (if staging is needed)
           - pushes stage change notification request into update queue

     class Updater
       - competes for CU state update reqeusts from update queue
       - for each CU
         - pushes state update (collected into bulks if possible)
         - cleans CU workdir if CU is final and cleanup is requested

     Agent
       |
       +--------------------------------------------------------
       |           |              |              |             |
       |           |              |              |             |
       V           V              V              V             V
     ExecWorker* StageinWorker* StageoutWorker* UpdateWorker* HeartbeatMonitor
       |
       +-------------------------------------------------
       |     |               |                |         |
       |     |               |                |         |
       V     V               V                V         V
     LRMS  MPILaunchMethod TaskLaunchMethod Scheduler Spawner


    NOTE:
    -----
      - Units are progressing through the different worker threads, where, in
        general, the unit changes state when transitioning to the next thread.
        The unit ownership thus *defines* the unit state (its owned by the
        InputStagingWorker, it is in StagingInput state, etc), and the state
        update notifications to the DB are merely informational (and can thus be
        asynchron).  The updates need to be ordered though, to reflect valid and
        correct state transition history.


    TODO:
    -----

    - add option to scheduler to ignore core 0 (which hosts the agent process)
    - add LRMS.partition (n) to return a set of partitioned LRMS for partial
      ExecWorkers
    - publish pilot slot history once on shutdown?  Or once in a while when
      idle?  Or push continuously?
    - Schedulers, LRMSs, LaunchMethods, etc need to be made threadsafe, for the
      case where more than one execution worker threads are running.
    - move util functions to rp.utils or r.utils, and pull the from there
    - split the agent into logical components (classes?), and install along with
      RP.
    - add state asserts after `queue.get ()`

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
import traceback
import threading
import subprocess
import multiprocessing

import radical.utils as ru
import radical.pilot as rp

from operator import mul


# ------------------------------------------------------------------------------
#
# http://stackoverflow.com/questions/9539052/python-dynamically-changing-base-classes-at-runtime-how-to
#
# Depending on agent architecture (which is specific to the resource type it
# runs on) can switch between different component types: using threaded (when
# running on the same node), multiprocessing (also for running on the same node,
# but avoiding python's threading problems, for the prices of slower queues),
# and remote processes (for running components on different nodes, using zeromq
# queues for communication).
#
# We do some trickery to keep the actual components independent from the actual
# schema:
#
#   - we wrap the different queue types into a rpu.Queue object
#   - we change the base class of the component dynamically to the respective type
#
# This requires components to adhere to the following restrictions:
#
#   - *only* communicate over queues -- no shared data with other components or
#     component instances.  Note that this also holds for example for the
#     scheduler!
#   - no shared data between the component class and it's run() method.  That
#     includes no sharing of queues.
#   - components inherit from base_component, and the constructor needs to
#     register all required component-internal and -external queues with that
#     base class -- the run() method can then transparently retrieve them from
#     there.
#

AGENT_THREADS   = 'threading'
AGENT_PROCESSES = 'multiprocessing'

AGENT_MODE      = AGENT_THREADS

if AGENT_MODE == AGENT_THREADS :
    COMPONENT_MODE = threading
    COMPONENT_TYPE = threading.Thread
    QUEUE_TYPE     = multiprocessing.Queue
elif AGENT_MODE == AGENT_PROCESSES :
    COMPONENT_MODE = multiprocessing
    COMPONENT_TYPE = multiprocessing.Process
    QUEUE_TYPE     = multiprocessing.Queue
    


# this needs git attribute 'ident' set for this file
git_ident = "$Id$"


# ------------------------------------------------------------------------------
#
# DEBUGGING CONSTANTS -- only change when you know what you are doing.  It is
# almost guaranteed that any changes will make the agent non-functional (if
# functionality is definied as executing a set of given CUs).

# component IDs

INGEST            = 'INGEST'
STAGEIN           = 'STAGEIN'
SCHEDULE          = 'SCHEDULE'
EXEC              = 'EXEC'
WATCH             = 'WATCH'
STAGEOUT          = 'STAGEOUT'
UPDATE            = 'UPDATE'

# Number of worker threads
NUMBER_OF_WORKERS = {
        INGEST   : 1,
        STAGEIN  : 1,
        SCHEDULE : 1,
        EXEC     : 1,
        WATCH    : 1,
        STAGEOUT : 1,
        UPDATE   : 1
}

# factor by which the number of units are increased at a certain step.  Value of
# '1' will leave the units unchanged.  Any blowup will leave on unit as the
# original, and will then create clones with an changed unit ID (see blowup()).
BLOWUP_FACTOR = {
        INGEST    :   1,
        STAGEIN   :   1,
        SCHEDULE  :   1,
        EXEC      :   1,
        WATCH     :   1,
        STAGEOUT  :   1,
        UPDATE    :   1
}

# flag to drop all blown-up units at some point in the pipeline.  The units
# with the original IDs will again be left untouched, but all other units are
# silently discarded.
DROP_CLONES = {
        INGEST    : True,
        STAGEIN   : True,
        SCHEDULE  : True,
        EXEC      : True,
        WATCH     : True,
        STAGEOUT  : True,
        UPDATE    : True
}
# 
# ------------------------------------------------------------------------------

# ------------------------------------------------------------------------------
# CONSTANTS
#
N_STAGEIN_WORKER            = 1
N_EXEC_WORKER               = 1
N_WATCH_WORKER              = 1
N_STAGEOUT_WORKER           = 1
N_UPDATE_WORKER             = 1

# 'enum' for unit launch method types
LAUNCH_METHOD_APRUN         = 'APRUN'
LAUNCH_METHOD_CCMRUN        = 'CCMRUN'
LAUNCH_METHOD_DPLACE        = 'DPLACE'
LAUNCH_METHOD_FORK          = 'FORK'
LAUNCH_METHOD_IBRUN         = 'IBRUN'
LAUNCH_METHOD_MPIEXEC       = 'MPIEXEC'
LAUNCH_METHOD_MPIRUN_CCMRUN = 'MPIRUN_CCMRUN'
LAUNCH_METHOD_MPIRUN_DPLACE = 'MPIRUN_DPLACE'
LAUNCH_METHOD_MPIRUN        = 'MPIRUN'
LAUNCH_METHOD_MPIRUN_RSH    = 'MPIRUN_RSH'
LAUNCH_METHOD_POE           = 'POE'
LAUNCH_METHOD_RUNJOB        = 'RUNJOB'
LAUNCH_METHOD_SSH           = 'SSH'

# 'enum' for local resource manager types
LRMS_NAME_FORK              = 'FORK'
LRMS_NAME_LOADLEVELER       = 'LOADL'
LRMS_NAME_LSF               = 'LSF'
LRMS_NAME_PBSPRO            = 'PBSPRO'
LRMS_NAME_SGE               = 'SGE'
LRMS_NAME_SLURM             = 'SLURM'
LRMS_NAME_TORQUE            = 'TORQUE'

# 'enum' for pilot's unit scheduler types
SCHEDULER_NAME_CONTINUOUS   = "CONTINUOUS"
SCHEDULER_NAME_SCATTERED    = "SCATTERED"
SCHEDULER_NAME_TORUS        = "TORUS"

# 'enum' for pilot's unit spawner types
SPAWNER_NAME_POPEN          = "POPEN"
SPAWNER_NAME_SHELL          = "SHELL"

# defines for pilot commands
COMMAND_CANCEL_PILOT        = "Cancel_Pilot"
COMMAND_CANCEL_COMPUTE_UNIT = "Cancel_Compute_Unit"
COMMAND_KEEP_ALIVE          = "Keep_Alive"
COMMAND_FIELD               = "commands"
COMMAND_TYPE                = "type"
COMMAND_ARG                 = "arg"
COMMAND_RESCHEDULE          = "Reschedule"
COMMAND_CANCEL              = "Cancel"


# 'enum' for staging action operators
COPY     = 'Copy'     # local cp
LINK     = 'Link'     # local ln -s
MOVE     = 'Move'     # local mv
TRANSFER = 'Transfer' # saga remote transfer
                      # TODO: This might just be a special case of copy

# tri-state for unit spawn retval
OK       = 'OK'
FAIL     = 'FAIL'
RETRY    = 'RETRY'

# two-state for slot occupation.
FREE     = 'Free'
BUSY     = 'Busy'

# directory for staging files inside the agent sandbox
STAGING_AREA         = 'staging_area'

# max number of cu out/err chars to push to db
MAX_IO_LOGLENGTH     = 1*1024

# max time period to collec db requests into bulks (seconds)
BULK_COLLECTION_TIME = 1.0

# time to sleep between queue polls (seconds)
QUEUE_POLL_SLEEPTIME = 0.1

# time to sleep between database polls (seconds)
DB_POLL_SLEEPTIME    = 0.5

# time between checks of internal state and commands from mothership (seconds)
HEARTBEAT_INTERVAL   = 10


# ------------------------------------------------------------------------------
#
# time stamp for profiling etc.
#
def timestamp():
    # human readable absolute UTC timestamp for log entries in database
    return datetime.datetime.utcnow()

def timestamp_epoch():
    # absolute timestamp as seconds since epoch
    return float(time.time())

# absolute timestamp in seconds since epocj pointing at start of
# bootstrapper (or 'now' as fallback)
timestamp_zero = float(os.environ.get('TIME_ZERO', time.time()))

print "timestamp zero: %s" % timestamp_zero

def timestamp_now():
    # relative timestamp seconds since TIME_ZERO (start)
    return float(time.time()) - timestamp_zero


# ------------------------------------------------------------------------------
#
# profiling support
#
# If 'RADICAL_PILOT_PROFILE' is set in environment, the agent logs timed events.
#
if 'RADICAL_PILOT_PROFILE' in os.environ:
    profile_agent  = True
    profile_handle = open('agent.prof', 'a')
else:
    profile_agent  = False
    profile_handle = sys.stdout


# ------------------------------------------------------------------------------
#
profile_tags  = dict()
profile_freqs = dict()

def prof(etype, uid="", msg="", tag="", logger=None):

    # record a timed event.  We record the thread ID, the uid of the affected
    # object, a log message, event type, and a tag.  Whenever a tag changes (to
    # a non-None value), the time since the last tag change is added.  This can
    # be used to derive, for example, the duration which a uid spent in
    # a certain state.  Time intervals between the same tags (but different
    # uids) are recorded, too.
    #
    # TODO: should this move to utils?  Or at least RP utils, so that we can
    # also use it for the application side?

    if logger:
        logger("%s -- %s (%s): %s", etype, msg, uid, tag)


    if not profile_agent:
        return


    logged = False
    now    = timestamp_now()

    if   AGENT_MODE == AGENT_THREADS   : tid = threading.current_thread().name
    elif AGENT_MODE == AGENT_PROCESSES : tid = os.getpid ()

    if uid and tag:

        if not uid in profile_tags:
            profile_tags[uid] = {'tag'  : "",
                                 'time' : 0.0 }

        old_tag = profile_tags[uid]['tag']

        if tag != old_tag:

            tagged_time = now - profile_tags[uid]['time']

            profile_tags[uid]['tag' ] = tag
            profile_tags[uid]['time'] = timestamp_now()

            profile_handle.write("> %12.4f : %-20s : %12.4f : %-17s : %-24s : %-40s : %s\n" \
                                 % (tagged_time, tag, now, tid, uid, etype, msg))
            logged = True


            if not tag in profile_freqs:
                profile_freqs[tag] = {'last'  : now,
                                      'diffs' : list()}
            else:
                diff = now - profile_freqs[tag]['last']
                profile_freqs[tag]['diffs'].append(diff)
                profile_freqs[tag]['last' ] = now

              # freq = sum(profile_freqs[tag]['diffs']) / len(profile_freqs[tag]['diffs'])
              #
              # profile_handle.write("> %12s : %-20.4f : %12s : %-17s : %-24s : %-40s : %s\n" \
              #                      % ('frequency', freq, '', '', '', '', ''))



    if not logged:
        profile_handle.write("  %12s : %-20s : %12.4f : %-17s : %-24s : %-40s : %s\n" \
                             % (' ' , ' ', now, tid, uid, etype, msg))
  
    # FIXME: disable flush on production runs
    profile_handle.flush()



# ------------------------------------------------------------------------------
#
def tail(txt, maxlen=MAX_IO_LOGLENGTH):

    # shorten the given string to the last <n> characters, and prepend
    # a notification.  This is used to keep logging information in mongodb
    # manageable(the size of mongodb documents is limited).

    if not txt:
        return txt

    if len(txt) > maxlen:
        return "[... CONTENT SHORTENED ...]\n%s" % txt[-maxlen:]
    else:
        return txt


# ------------------------------------------------------------------------------
#
def blowup(cus, component):
    # for each cu in cu_list, add 'factor' clones just like it, just with
    # a different ID (<id>.clone_001)

    if not isinstance (cus, list) :
        cus = [cus]

    if not profile_agent:
        return cus

    factor = BLOWUP_FACTOR.get (component, 1)
    drop   = DROP_CLONES  .get (component, False)

    ret = list()

    for cu in cus :

        uid = cu['_id']

        if drop :
            if '.clone_' in uid :
                prof ('drop', uid=uid)
                continue
        
        factor -= 1
        if factor :
            for idx in range(factor) :

                cu_clone = copy.deepcopy (dict(cu))
                clone_id = '%s.clone_%05d' % (str(cu['_id']), idx+1)

                for key in cu_clone :
                    if isinstance (cu_clone[key], basestring) :
                        cu_clone[key] = cu_clone[key].replace (uid, clone_id)

                idx += 1
                ret.append (cu_clone)
                prof('cloned unit ingest (%s)' % component, uid=clone_id)

        # append the original unit last, to  increase the likelyhood that
        # application state only advances once all clone states have also
        # advanced (they'll get pushed onto queues earlier)
        ret.append (cu)

    return ret


# ------------------------------------------------------------------------------
#
def get_rusage():

    import resource

    self_usage  = resource.getrusage(resource.RUSAGE_SELF)
    child_usage = resource.getrusage(resource.RUSAGE_CHILDREN)

    rtime = time.time() - timestamp_zero
    utime = self_usage.ru_utime  + child_usage.ru_utime
    stime = self_usage.ru_stime  + child_usage.ru_stime
    rss   = self_usage.ru_maxrss + child_usage.ru_maxrss

    return "real %3f sec | user %.3f sec | system %.3f sec | mem %.2f kB" \
         % (rtime, utime, stime, rss)


# ----------------------------------------------------------------------------------
#
def rec_makedir(target):

    # recursive makedir which ignores errors if dir already exists

    try:
        os.makedirs(target)
    except OSError as e:
        # ignore failure on existing directory
        if e.errno == errno.EEXIST and os.path.isdir(os.path.dirname(target)):
            pass
        else:
            raise


# ------------------------------------------------------------------------------
#
def get_mongodb(mongodb_url, mongodb_name, mongodb_auth):

    mongo_client = pymongo.MongoClient(mongodb_url)
    mongo_db     = mongo_client[mongodb_name]

    # do auth on username *and* password (ignore empty split results)
    if mongodb_auth:
        username, passwd = mongodb_auth.split(':')
        mongo_db.authenticate(username, passwd)

    return mongo_db


# ------------------------------------------------------------------------------
#
def pilot_FAILED(mongo_p, pilot_uid, logger, message):

    logger.error(message)

    now = timestamp()
    out = None
    err = None
    log = None

    try    : out = open('./agent.out', 'r').read()
    except : pass
    try    : err = open('./agent.err', 'r').read()
    except : pass
    try    : log = open('./agent.log',    'r').read()
    except : pass

    msg = [{"message": message,      "timestamp": now},
           {"message": get_rusage(), "timestamp": now}]

    if mongo_p:
        mongo_p.update({"_id": pilot_uid},
            {"$pushAll": {"log"         : msg},
             "$push"   : {"statehistory": {"state"     : rp.FAILED,
                                           "timestamp" : now}},
             "$set"    : {"state"       : rp.FAILED,
                          "stdout"      : tail(out),
                          "stderr"      : tail(err),
                          "logfile"     : tail(log),
                          "finished"    : now}
            })

    else:
        logger.error("cannot log error state in database!")


# ------------------------------------------------------------------------------
#
def pilot_CANCELED(mongo_p, pilot_uid, logger, message):

    logger.warning(message)

    now = timestamp()
    out = None
    err = None
    log = None

    try    : out = open('./agent.out', 'r').read()
    except : pass
    try    : err = open('./agent.err', 'r').read()
    except : pass
    try    : log = open('./agent.log',    'r').read()
    except : pass

    msg = [{"message": message,      "timestamp": now},
           {"message": get_rusage(), "timestamp": now}]

    mongo_p.update({"_id": pilot_uid},
        {"$pushAll": {"log"         : msg},
         "$push"   : {"statehistory": {"state"     : rp.CANCELED,
                                       "timestamp" : now}},
         "$set"    : {"state"       : rp.CANCELED,
                      "stdout"      : tail(out),
                      "stderr"      : tail(err),
                      "logfile"     : tail(log),
                      "finished"    : now}
        })


# ------------------------------------------------------------------------------
#
def pilot_DONE(mongo_p, pilot_uid):

    now = timestamp()
    out = None
    err = None
    log = None

    try    : out = open('./agent.out', 'r').read()
    except : pass
    try    : err = open('./agent.err', 'r').read()
    except : pass
    try    : log = open('./agent.log',    'r').read()
    except : pass

    msg = [{"message": "pilot done", "timestamp": now},
           {"message": get_rusage(), "timestamp": now}]

    mongo_p.update({"_id": pilot_uid},
        {"$pushAll": {"log"         : msg},
         "$push"   : {"statehistory": {"state"    : rp.DONE,
                                       "timestamp": now}},
         "$set"    : {"state"       : rp.DONE,
                      "stdout"      : tail(out),
                      "stderr"      : tail(err),
                      "logfile"     : tail(log),
                      "finished"    : now}
        })


# ==============================================================================
#
# Schedulers
#
# ==============================================================================
#
class Scheduler(threading.Thread):

    # FIXME: clarify what can be overloaded by Scheduler classes

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, lrms, schedule_queue, execution_queue,
                 update_queue):

        threading.Thread.__init__(self)

        self.name             = name
        self._log             = logger
        self._lrms            = lrms
        self._schedule_queue  = schedule_queue
        self._execution_queue = execution_queue
        self._update_queue    = update_queue

        self._terminate       = threading.Event()
        self._lock            = threading.RLock()
        self._wait_queue      = list()

        self._configure()


    # --------------------------------------------------------------------------
    #
    # This class-method creates the appropriate sub-class for the Launch Method.
    #
    @classmethod
    def create(cls, logger, name, lrms, schedule_queue, execution_queue,
               update_queue):

        # Make sure that we are the base-class!
        if cls != Scheduler:
            raise Exception("Scheduler Factory only available to base class!")

        try:
            implementation = {
                SCHEDULER_NAME_CONTINUOUS : SchedulerContinuous,
                SCHEDULER_NAME_SCATTERED  : SchedulerScattered,
                SCHEDULER_NAME_TORUS      : SchedulerTorus
            }[name]

            impl = implementation(name, logger, lrms, schedule_queue,
                                  execution_queue, update_queue)

            impl.start()
            return impl

        except KeyError:
            raise Exception("Scheduler '%s' unknown!" % name)


    # --------------------------------------------------------------------------
    #
    def stop(self):
        self._terminate.set()


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        raise NotImplementedError("_configure() not implemented for Scheduler '%s'." % self.name)


    # --------------------------------------------------------------------------
    #
    def slot_status(self, short=False):
        raise NotImplementedError("slot_status() not implemented for Scheduler '%s'." % self.name)


    # --------------------------------------------------------------------------
    #
    def _allocate_slot(self, cores_requested):
        raise NotImplementedError("_allocate_slot() not implemented for Scheduler '%s'." % self.name)


    # --------------------------------------------------------------------------
    #
    def _release_slot(self, opaque_slot):
        raise NotImplementedError("_release_slot() not implemented for Scheduler '%s'." % self.name)


    # --------------------------------------------------------------------------
    #
    def _try_allocation(self, cu):

        # needs to be locked as we try to acquire slots, but slots are freed 
        # in a different thread...
        with self._lock :

            # schedule this unit, and receive an opaque handle that has meaning to
            # the LRMS, Scheduler and LaunchMethod.
            cu['opaque_slot'] = self._allocate_slot(cu['description']['cores'])

            if cu['opaque_slot']:
                # got an allocation, go off and launch the process
                # FIXME: state update toward EXECUTING (or is that done in
                # launcher?)
                prof('schedule', msg="allocated", uid=cu['_id'])
                cu_list = blowup (cu, EXEC) 
                for _cu in cu_list :
                    prof('push', msg="towards execution", uid=_cu['_id'])
                    self._execution_queue.put(_cu)
                return True

            else:
                # otherwise signal that CU remains unhandled
                return False


    # --------------------------------------------------------------------------
    #
    def _reschedule(self):

        prof("try reschedule")
        prof('reschedule')
        prof(self.slot_status (short=True))
        # cycle through wait queue, and see if we get anything running now.  We
        # cycle over a copy of the list, so that we can modify the list on the
        # fly
        for cu in self._wait_queue[:]:

            if self._try_allocation(cu):
                # yep, that worked - remove it from the wait queue
                self._wait_queue.remove(cu)
                prof('unqueue', msg="re-allocation done", uid=cu['_id'])

        prof(self.slot_status (short=True))
        prof('reschedule done')


    # --------------------------------------------------------------------------
    #
    def unschedule(self, cus):
        # release (for whatever reason) all slots allocated to this CU

        # needs to be locked as we try to release slots, but slots are acquired
        # in a different thread....
        with self._lock :

            prof('unschedule')
            prof(self.slot_status (short=True))

            slots_released = False

            if not isinstance(cus, list):
                cus = [cus]

            for cu in cus:
                if cu['opaque_slot']:
                    self._release_slot(cu['opaque_slot'])
                    slots_released = True

            # notify the scheduling thread of released slots
            if slots_released:
                self._schedule_queue.put(COMMAND_RESCHEDULE)

            prof(self.slot_status (short=True))
            prof('unschedule done - reschedule')


    # --------------------------------------------------------------------------
    #
    def run(self):

        self._log.info("started %s.", self)

        while not self._terminate.is_set():

            try:

                request = self._schedule_queue.get()

                # shutdown signal
                if not request:
                    continue

                # we either get a new scheduled CU, or get a trigger that cores were
                # freed, and we can try to reschedule waiting CUs
                if isinstance(request, basestring):

                    command = request
                    if command == COMMAND_RESCHEDULE:
                        self._reschedule()

                    else:
                        self._log.error("Unknown scheduler command: %s (ignored)", command)

                else:


                    # we got a new unit.  Either we can place it straight away and
                    # move it to execution, or we have to put it on the wait queue
                    cu = request
                    prof('schedule', msg="unit received", uid=cu['_id'])
                    if not self._try_allocation(cu):
                        # No resources available, put in wait queue
                        self._wait_queue.append(cu)
                        prof('queue', msg="allocation failed", uid=cu['_id'])


            except Exception as e:
                self._log.exception('Error in scheduler loop: %s', e)
                raise


# ==============================================================================
#
class SchedulerContinuous(Scheduler):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, lrms, scheduler_queue,
                 execution_queue, update_queue):

        self.slots = None

        Scheduler.__init__(self, name, logger, lrms, scheduler_queue,
                execution_queue, update_queue)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        if not self._lrms.node_list:
            raise Exception("LRMS %s didn't _configure node_list." % self._lrms.name)

        if not self._lrms.cores_per_node:
            raise Exception("LRMS %s didn't _configure cores_per_node." % self._lrms.name)

        # Slots represents the internal process management structure.
        # The structure is as follows:
        # [
        #    {'node': 'node1', 'cores': [p_1, p_2, p_3, ... , p_cores_per_node]},
        #    {'node': 'node2', 'cores': [p_1, p_2, p_3. ... , p_cores_per_node]
        # ]
        #
        # We put it in a list because we care about (and make use of) the order.
        #
        self.slots = []
        for node in self._lrms.node_list:
            self.slots.append({
                'node': node,
                # TODO: Maybe use the real core numbers in the case of
                # non-exclusive host reservations?
                'cores': [FREE for _ in range(0, self._lrms.cores_per_node)]
            })


    # --------------------------------------------------------------------------
    #
    # Convert a set of slots into an index into the global slots list
    #
    def slots2offset(self, task_slots):
        # TODO: This assumes all hosts have the same number of cores

        first_slot = task_slots[0]
        # Get the host and the core part
        [first_slot_host, first_slot_core] = first_slot.split(':')
        # Find the entry in the the all_slots list based on the host
        slot_entry = (slot for slot in self.slots if slot["node"] == first_slot_host).next()
        # Transform it into an index in to the all_slots list
        all_slots_slot_index = self.slots.index(slot_entry)

        return all_slots_slot_index * self._lrms.cores_per_node + int(first_slot_core)


    # --------------------------------------------------------------------------
    #
    def slot_status(self, short=False):
        """Returns a multi-line string corresponding to slot status.
        """

        if short:
            slot_matrix = ""
            for slot in self.slots:
                slot_matrix += "|"
                for core in slot['cores']:
                    if core == FREE:
                        slot_matrix += "-"
                    else:
                        slot_matrix += "+"
            slot_matrix += "|"
            return {'timestamp' : timestamp(),
                    'slotstate' : slot_matrix}

        else:
            slot_matrix = ""
            for slot in self.slots:
                slot_vector  = ""
                for core in slot['cores']:
                    if core == FREE:
                        slot_vector += " - "
                    else:
                        slot_vector += " X "
                slot_matrix += "%-24s: %s\n" % (slot['node'], slot_vector)
            return slot_matrix


    # --------------------------------------------------------------------------
    #
    # (Temporary?) wrapper for acquire_slots
    #
    def _allocate_slot(self, cores_requested):

        # TODO: single_node should be enforced for e.g. non-message passing
        #       tasks, but we don't have that info here.
        # NOTE AM: why should non-messaging tasks be confined to one node?
        if cores_requested < self._lrms.cores_per_node:
            single_node = True
        else:
            single_node = False

        # Given that we are the continuous scheduler, this is fixed.
        # TODO: Argument can be removed altogether?
        continuous = True

        # TODO: Now we rely on "None", maybe throw an exception?
        return self._acquire_slots(cores_requested, single_node=single_node,
                continuous=continuous)


    # --------------------------------------------------------------------------
    #
    def _release_slot(self, (task_slots)):
        self._change_slot_states(task_slots, FREE)


    # --------------------------------------------------------------------------
    #
    def _acquire_slots(self, cores_requested, single_node, continuous):

        #
        # Switch between searching for continuous or scattered slots
        #
        # Switch between searching for single or multi-node
        if single_node:
            if continuous:
                task_slots = self._find_slots_single_cont(cores_requested)
            else:
                raise NotImplementedError('No scattered single node scheduler implemented yet.')
        else:
            if continuous:
                task_slots = self._find_slots_multi_cont(cores_requested)
            else:
                raise NotImplementedError('No scattered multi node scheduler implemented yet.')

        if task_slots is not None:
            self._change_slot_states(task_slots, BUSY)

        return task_slots


    # --------------------------------------------------------------------------
    #
    # Find a needle (continuous sub-list) in a haystack (list)
    #
    def _find_sublist(self, haystack, needle):
        n = len(needle)
        # Find all matches (returns list of False and True for every position)
        hits = [(needle == haystack[i:i+n]) for i in xrange(len(haystack)-n+1)]
        try:
            # Grab the first occurrence
            index = hits.index(True)
        except ValueError:
            index = None

        return index


    # --------------------------------------------------------------------------
    #
    # Transform the number of cores into a continuous list of "status"es,
    # and use that to find a sub-list.
    #
    def _find_cores_cont(self, slot_cores, cores_requested, status):
        return self._find_sublist(slot_cores, [status for _ in range(cores_requested)])


    # --------------------------------------------------------------------------
    #
    # Find an available continuous slot within node boundaries.
    #
    def _find_slots_single_cont(self, cores_requested):

        for slot in self.slots:
            slot_node = slot['node']
            slot_cores = slot['cores']

            slot_cores_offset = self._find_cores_cont(slot_cores, cores_requested, FREE)

            if slot_cores_offset is not None:
                self._log.info('Node %s satisfies %d cores at offset %d',
                              slot_node, cores_requested, slot_cores_offset)
                return ['%s:%d' % (slot_node, core) for core in
                        range(slot_cores_offset, slot_cores_offset + cores_requested)]

        return None


    # --------------------------------------------------------------------------
    #
    # Find an available continuous slot across node boundaries.
    #
    def _find_slots_multi_cont(self, cores_requested):

        # Convenience aliases
        cores_per_node = self._lrms.cores_per_node
        all_slots = self.slots

        # Glue all slot core lists together
        all_slot_cores = [core for node in [node['cores'] for node in all_slots] for core in node]
        # self._log.debug("all_slot_cores: %s", all_slot_cores)

        # Find the start of the first available region
        all_slots_first_core_offset = self._find_cores_cont(all_slot_cores, cores_requested, FREE)
        self._log.debug("all_slots_first_core_offset: %s", all_slots_first_core_offset)
        if all_slots_first_core_offset is None:
            return None

        # Determine the first slot in the slot list
        first_slot_index = all_slots_first_core_offset / cores_per_node
        self._log.debug("first_slot_index: %s", first_slot_index)
        # And the core offset within that node
        first_slot_core_offset = all_slots_first_core_offset % cores_per_node
        self._log.debug("first_slot_core_offset: %s", first_slot_core_offset)

        # Note: We subtract one here, because counting starts at zero;
        #       Imagine a zero offset and a count of 1, the only core used
        #       would be core 0.
        #       TODO: Verify this claim :-)
        all_slots_last_core_offset = (first_slot_index * cores_per_node) +\
                                     first_slot_core_offset + cores_requested - 1
        self._log.debug("all_slots_last_core_offset: %s", all_slots_last_core_offset)
        last_slot_index = (all_slots_last_core_offset) / cores_per_node
        self._log.debug("last_slot_index: %s", last_slot_index)
        last_slot_core_offset = all_slots_last_core_offset % cores_per_node
        self._log.debug("last_slot_core_offset: %s", last_slot_core_offset)

        # Convenience aliases
        last_slot = self.slots[last_slot_index]
        self._log.debug("last_slot: %s", last_slot)
        last_node = last_slot['node']
        self._log.debug("last_node: %s", last_node)
        first_slot = self.slots[first_slot_index]
        self._log.debug("first_slot: %s", first_slot)
        first_node = first_slot['node']
        self._log.debug("first_node: %s", first_node)

        # Collect all node:core slots here
        task_slots = []

        # Add cores from first slot for this unit
        # As this is a multi-node search, we can safely assume that we go
        # from the offset all the way to the last core.
        task_slots.extend(['%s:%d' % (first_node, core) for core in
                           range(first_slot_core_offset, cores_per_node)])

        # Add all cores from "middle" slots
        for slot_index in range(first_slot_index+1, last_slot_index):
            slot_node = all_slots[slot_index]['node']
            task_slots.extend(['%s:%d' % (slot_node, core) for core in range(0, cores_per_node)])

        # Add the cores of the last slot
        task_slots.extend(['%s:%d' % (last_node, core) for core in range(0, last_slot_core_offset+1)])

        return task_slots


    # --------------------------------------------------------------------------
    #
    # Change the reserved state of slots (FREE or BUSY)
    #
    def _change_slot_states(self, task_slots, new_state):

        # Convenience alias
        all_slots = self.slots

        # logger.debug("change_slot_states: unit slots: %s", task_slots)

        for slot in task_slots:
            # logger.debug("change_slot_states: slot content: %s", slot)
            # Get the node and the core part
            [slot_node, slot_core] = slot.split(':')
            # Find the entry in the the all_slots list
            slot_entry = (slot for slot in all_slots if slot["node"] == slot_node).next()
            # Change the state of the slot
            slot_entry['cores'][int(slot_core)] = new_state



# ==============================================================================
#
class SchedulerScattered(Scheduler):
    # FIXME: implement
    pass


# ==============================================================================
#
class SchedulerTorus(Scheduler):

    # TODO: Ultimately all BG/Q specifics should move out of the scheduler

    # --------------------------------------------------------------------------
    #
    # Offsets into block structure
    #
    TORUS_BLOCK_INDEX  = 0
    TORUS_BLOCK_COOR   = 1
    TORUS_BLOCK_NAME   = 2
    TORUS_BLOCK_STATUS = 3 


    # --------------------------------------------------------------------------
    def __init__(self, name, logger, lrms, scheduler_queue,
                 execution_queue, update_queue):

        self.slots            = None
        self._cores_per_node  = None

        Scheduler.__init__(self, name, logger, lrms, scheduler_queue,
                execution_queue, update_queue)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        if not self._lrms.cores_per_node:
            raise Exception("LRMS %s didn't _configure cores_per_node." % self._lrms.name)

        self._cores_per_node = self._lrms.cores_per_node

        # TODO: get rid of field below
        self.slots = 'bogus'


    # --------------------------------------------------------------------------
    #
    def slot_status(self, short=False):
        """Returns a multi-line string corresponding to slot status.
        """
        # TODO: Both short and long currently only deal with full-node status
        if short:
            slot_matrix = ""
            for slot in self._lrms.torus_block:
                slot_matrix += "|"
                if slot[self.TORUS_BLOCK_STATUS] == FREE:
                    slot_matrix += "-" * self._lrms.cores_per_node
                else:
                    slot_matrix += "+" * self._lrms.cores_per_node
            slot_matrix += "|"
            return {'timestamp': timestamp(),
                    'slotstate': slot_matrix}
        else:
            slot_matrix = ""
            for slot in self._lrms.torus_block:
                slot_vector = ""
                if slot[self.TORUS_BLOCK_STATUS] == FREE:
                    slot_vector = " - " * self._lrms.cores_per_node
                else:
                    slot_vector = " X " * self._lrms.cores_per_node
                slot_matrix += "%s: %s\n" % (slot[self.TORUS_BLOCK_NAME].ljust(24), slot_vector)
            return slot_matrix


    # --------------------------------------------------------------------------
    #
    # Allocate a number of cores
    #
    # Currently only implements full-node allocation, so core count must
    # be a multiple of cores_per_node.
    #
    def _allocate_slot(self, cores_requested):

        block = self._lrms.torus_block
        sub_block_shape_table = self._lrms.shape_table

        self._log.info("Trying to allocate %d core(s).", cores_requested)

        if cores_requested % self._lrms.cores_per_node:
            num_cores = int(math.ceil(cores_requested / float(self._lrms.cores_per_node))) \
                        * self._lrms.cores_per_node
            self._log.error('Core not multiple of %d, increasing to %d!',
                           self._lrms.cores_per_node, num_cores)

        num_nodes = cores_requested / self._lrms.cores_per_node

        offset = self._alloc_sub_block(block, num_nodes)

        if offset is None:
            self._log.warning('No allocation made.')
            return

        # TODO: return something else than corner location? Corner index?
        corner = block[offset][self.TORUS_BLOCK_COOR]
        sub_block_shape = sub_block_shape_table[num_nodes]

        end = self.get_last_node(corner, sub_block_shape)
        self._log.debug('Allocating sub-block of %d node(s) with dimensions %s'
                       ' at offset %d with corner %s and end %s.',
                        num_nodes, self._lrms.shape2str(sub_block_shape), offset,
                        self._lrms.loc2str(corner), self._lrms.loc2str(end))

        return corner, sub_block_shape


    # --------------------------------------------------------------------------
    #
    # Allocate a sub-block within a block
    # Currently only works with offset that are exactly the sub-block size
    #
    def _alloc_sub_block(self, block, num_nodes):

        offset = 0
        # Iterate through all nodes with offset a multiple of the sub-block size
        while True:

            # Verify the assumption (needs to be an assert?)
            if offset % num_nodes != 0:
                msg = 'Sub-block needs to start at correct offset!'
                self._log.exception(msg)
                raise Exception(msg)
                # TODO: If we want to workaround this, the coordinates need to overflow

            not_free = False
            # Check if all nodes from offset till offset+size are FREE
            for peek in range(num_nodes):
                try:
                    if block[offset+peek][self.TORUS_BLOCK_STATUS] == BUSY:
                        # Once we find the first BUSY node we can discard this attempt
                        not_free = True
                        break
                except IndexError:
                    self._log.exception('Block out of bound. Num_nodes: %d, offset: %d, peek: %d.',
                            num_nodes, offset, peek)

            if not_free == True:
                # No success at this offset
                self._log.info("No free nodes found at this offset: %d.", offset)

                # If we weren't the last attempt, then increase the offset and iterate again.
                if offset + num_nodes < self._block2num_nodes(block):
                    offset += num_nodes
                    continue
                else:
                    return

            else:
                # At this stage we have found a free spot!

                self._log.info("Free nodes found at this offset: %d.", offset)

                # Then mark the nodes busy
                for peek in range(num_nodes):
                    block[offset+peek][self.TORUS_BLOCK_STATUS] = BUSY

                return offset


    # --------------------------------------------------------------------------
    #
    # Return the number of nodes in a block
    #
    def _block2num_nodes(self, block):
        return len(block)


    # --------------------------------------------------------------------------
    #
    def _release_slot(self, (corner, shape)):
        self._free_cores(self._lrms.torus_block, corner, shape)


    # --------------------------------------------------------------------------
    #
    # Free up an allocation
    #
    def _free_cores(self, block, corner, shape):

        # Number of nodes to free
        num_nodes = self._shape2num_nodes(shape)

        # Location of where to start freeing
        offset = self.corner2offset(block, corner)

        self._log.info("Freeing %d nodes starting at %d.", num_nodes, offset)

        for peek in range(num_nodes):
            assert block[offset+peek][self.TORUS_BLOCK_STATUS] == BUSY, \
                'Block %d not Free!' % block[offset+peek]
            block[offset+peek][self.TORUS_BLOCK_STATUS] = FREE


    # --------------------------------------------------------------------------
    #
    # Follow coordinates to get the last node
    #
    def get_last_node(self, origin, shape):
        ret = {}
        for dim in self._lrms.torus_dimension_labels:
            ret[dim] = origin[dim] + shape[dim] -1
        return ret


    # --------------------------------------------------------------------------
    #
    # Return the number of nodes for the given block shape
    #
    def _shape2num_nodes(self, shape):

        nodes = 1
        for dim in self._lrms.torus_dimension_labels:
            nodes *= shape[dim]

        return nodes


    # --------------------------------------------------------------------------
    #
    # Return the offset into the node list from a corner
    #
    # TODO: Can this be determined instead of searched?
    #
    def corner2offset(self, block, corner):
        offset = 0

        for e in block:
            if corner == e[self.TORUS_BLOCK_COOR]:
                return offset
            offset += 1

        return offset



# ==============================================================================
#
# Launch Methods
#
# ==============================================================================
#
class LaunchMethod(object):

    # List of environment variables that designated Launch Methods should export
    EXPORT_ENV_VARIABLES = [
        'LD_LIBRARY_PATH',
        'PATH',
        'PYTHONPATH',
        'PYTHON_DIR',
    ]

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        self.name      = name
        self._log      = logger
        self._scheduler = scheduler

        self.launch_command = None
        self._configure()
        # TODO: This doesn't make too much sense for LM's that use multiple
        #       commands, perhaps this needs to move to per LM __init__.
        if self.launch_command is None:
            raise Exception("Launch command not found for LaunchMethod '%s'" % name)

        logger.info("Discovered launch command: '%s'.", self.launch_command)

    # --------------------------------------------------------------------------
    #
    # This class-method creates the appropriate sub-class for the Launch Method.
    #
    @classmethod
    def create(cls, name, scheduler, logger):

        # Make sure that we are the base-class!
        if cls != LaunchMethod:
            raise Exception("LaunchMethod factory only available to base class!")

        try:
            implementation = {
                LAUNCH_METHOD_APRUN         : LaunchMethodAPRUN,
                LAUNCH_METHOD_CCMRUN        : LaunchMethodCCMRUN,
                LAUNCH_METHOD_DPLACE        : LaunchMethodDPLACE,
                LAUNCH_METHOD_FORK          : LaunchMethodFORK,
                LAUNCH_METHOD_IBRUN         : LaunchMethodIBRUN,
                LAUNCH_METHOD_MPIEXEC       : LaunchMethodMPIEXEC,
                LAUNCH_METHOD_MPIRUN_CCMRUN : LaunchMethodMPIRUNCCMRUN,
                LAUNCH_METHOD_MPIRUN_DPLACE : LaunchMethodMPIRUNDPLACE,
                LAUNCH_METHOD_MPIRUN        : LaunchMethodMPIRUN,
                LAUNCH_METHOD_MPIRUN_RSH    : LaunchMethodMPIRUNRSH,
                LAUNCH_METHOD_POE           : LaunchMethodPOE,
                LAUNCH_METHOD_RUNJOB        : LaunchMethodRUNJOB,
                LAUNCH_METHOD_SSH           : LaunchMethodSSH
            }[name]
            return implementation(name, logger, scheduler)

        except KeyError:
            logger.exception("LaunchMethod '%s' unknown!" % name)

        except Exception as e:
            logger.exception("LaunchMethod cannot be used: %s!" % e)

        return None


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        raise NotImplementedError("_configure() not implemented for LaunchMethod: %s." % self.name)

    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, opaque_slot):
        raise NotImplementedError("construct_command() not implemented for LaunchMethod: %s." % self.name)


    # --------------------------------------------------------------------------
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


    # --------------------------------------------------------------------------
    #
    def _which(self, program):
        """Finds the location of an executable.
        Taken from:
        http://stackoverflow.com/questions/377017/test-if-executable-exists-in-python
        """
        # ----------------------------------------------------------------------
        #
        def is_exe(fpath):
            return os.path.isfile(fpath) and os.access(fpath, os.X_OK)

        fpath, _ = os.path.split(program)
        if fpath:
            if is_exe(program):
                return program
        else:
            for path in os.environ["PATH"].split(os.pathsep):
                exe_file = os.path.join(path, program)
                if is_exe(exe_file):
                    return exe_file
        return None


# ==============================================================================
#
class LaunchMethodFORK(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # "Regular" tasks
        self.launch_command = ''


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, opaque_slot):

        if task_args:
            command = " ".join([task_exec, task_args])
        else:
            command = task_exec

        return command, None



# ==============================================================================
#
class LaunchMethodMPIRUN(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        self.launch_command = self._find_executable([
            'mpirun',            # General case
            'mpirun_rsh',        # Gordon @ SDSC
            'mpirun-mpich-mp',   # Mac OSX MacPorts
            'mpirun-openmpi-mp'  # Mac OSX MacPorts
        ])


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        # Construct the hosts_string
        hosts_string = ",".join([slot.split(':')[0] for slot in task_slots])

        export_vars = ' '.join(['-x ' + var for var in self.EXPORT_ENV_VARIABLES if var in os.environ])

        mpirun_command = "%s %s -np %s -host %s %s" % (
            self.launch_command, export_vars, task_numcores, hosts_string, task_command)

        return mpirun_command, None


# ==============================================================================
#
class LaunchMethodSSH(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # Find ssh command
        command = self._which('ssh')

        if command is not None:

            # Some MPI environments (e.g. SGE) put a link to rsh as "ssh" into
            # the path.  We try to detect that and then use different arguments.
            if os.path.islink(command):

                target = os.path.realpath(command)

                if os.path.basename(target) == 'rsh':
                    self._log.info('Detected that "ssh" is a link to "rsh".')
                    return target

            command = '%s -o StrictHostKeyChecking=no' % command

        self.launch_command = command


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        if not launch_script_hop :
            raise ValueError ("LaunchMethodSSH.construct_command needs launch_script_hop!")

        # Get the host of the first entry in the acquired slot
        host = task_slots[0].split(':')[0]

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        # Command line to execute launch script via ssh on host
        ssh_hop_cmd = "%s %s %s" % (self.launch_command, host, launch_script_hop)

        # Special case, return a tuple that overrides the default command line.
        return task_command, ssh_hop_cmd



# ==============================================================================
#
class LaunchMethodMPIEXEC(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # mpiexec (e.g. on SuperMUC)
        self.launch_command = self._find_executable([
            'mpiexec',            # General case
            'mpiexec-mpich-mp',   # Mac OSX MacPorts
            'mpiexec-openmpi-mp'  # Mac OSX MacPorts
        ])

    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        # Construct the hosts_string
        hosts_string = ",".join([slot.split(':')[0] for slot in task_slots])

        # Construct the executable and arguments
        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        mpiexec_command = "%s -n %s -host %s %s" % (
            self.launch_command, task_numcores, hosts_string, task_command)

        return mpiexec_command, None


# ==============================================================================
#
class LaunchMethodAPRUN(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # aprun: job launcher for Cray systems
        self.launch_command= self._which('aprun')

        # TODO: ensure that only one concurrent aprun per node is executed!


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, opaque_slot):

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        aprun_command = "%s -n %d %s" % (self.launch_command, task_numcores, task_command)

        return aprun_command, None



# ==============================================================================
#
class LaunchMethodCCMRUN(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # ccmrun: Cluster Compatibility Mode (CCM) job launcher for Cray systems
        self.launch_command= self._which('ccmrun')


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, opaque_slot):

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        ccmrun_command = "%s -n %d %s" % (self.launch_command, task_numcores, task_command)

        return ccmrun_command, None



# ==============================================================================
#
class LaunchMethodMPIRUNCCMRUN(LaunchMethod):
    # TODO: This needs both mpirun and ccmrun

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)

        self.mpirun_command = self._which('mpirun')


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # ccmrun: Cluster Compatibility Mode job launcher for Cray systems
        self.launch_command= self._which('ccmrun')

        self.mpirun_command = self._which('mpirun')
        if not self.mpirun_command:
            raise Exception("mpirun not found!")


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        # Construct the hosts_string
        # TODO: is there any use in using $HOME/.crayccm/ccm_nodelist.$JOBID?
        hosts_string = ",".join([slot.split(':')[0] for slot in task_slots])

        export_vars = ' '.join(['-x ' + var for var in self.EXPORT_ENV_VARIABLES if var in os.environ])

        mpirun_ccmrun_command = "%s %s %s -np %d -host %s %s" % (
            self.launch_command, self.mpirun_command, export_vars,
            task_numcores, hosts_string, task_command)

        return mpirun_ccmrun_command, None



# ==============================================================================
#
class LaunchMethodRUNJOB(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # runjob: job launcher for IBM BG/Q systems, e.g. Joule
        self.launch_command= self._which('runjob')


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (corner, sub_block_shape)):

        if task_numcores % self._scheduler._lrms.cores_per_node:
            msg = "Num cores (%d) is not a multiple of %d!" % (
                task_numcores, self._scheduler._lrms.cores_per_node)
            self._log.exception(msg)
            raise Exception(msg)

        # Runjob it is!
        runjob_command = self.launch_command

        # Set the number of tasks/ranks per node
        # TODO: Currently hardcoded, this should be configurable,
        #       but I don't see how, this would be a leaky abstraction.
        runjob_command += ' --ranks-per-node %d' % min(self._scheduler._lrms.cores_per_node, task_numcores)

        # Run this subjob in the block communicated by LoadLeveler
        runjob_command += ' --block %s' % self._scheduler._lrms.loadl_bg_block

        corner_offset = self._scheduler.corner2offset(self._scheduler._lrms.torus_block, corner)
        corner_node = self._scheduler._lrms.torus_block[corner_offset][self._scheduler.TORUS_BLOCK_NAME]
        runjob_command += ' --corner %s' % corner_node

        # convert the shape
        runjob_command += ' --shape %s' % self._scheduler._lrms.shape2str(sub_block_shape)

        # runjob needs the full path to the executable
        if os.path.basename(task_exec) == task_exec:
            # Use `which` with back-ticks as the executable,
            # will be expanded in the shell script.
            task_exec = '`which %s`' % task_exec
            # Note: We can't use the expansion from here,
            #       as the pre-execs of the CU aren't run yet!!

        # And finally add the executable and the arguments
        # usage: runjob <runjob flags> : /bin/hostname -f
        runjob_command += ' : %s' % task_exec
        if task_args:
            runjob_command += ' %s' % task_args

        return runjob_command, None


# ==============================================================================
#
class LaunchMethodDPLACE(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # dplace: job launcher for SGI systems (e.g. on Blacklight)
        self.launch_command = self._which('dplace')


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        dplace_offset = self._scheduler.slots2offset(task_slots)

        dplace_command = "%s -c %d-%d %s" % (
            self.launch_command, dplace_offset,
            dplace_offset+task_numcores-1, task_command)

        return dplace_command, None



# ==============================================================================
#
class LaunchMethodMPIRUNRSH(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # mpirun_rsh (e.g. on Gordon@ SDSC)
        self.launch_command = self._which('mpirun_rsh')


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        # Construct the hosts_string ('h1 h2 .. hN')
        hosts_string = " ".join([slot.split(':')[0] for slot in task_slots])

        export_vars = ' '.join([var+"=$"+var for var in self.EXPORT_ENV_VARIABLES if var in os.environ])

        mpirun_rsh_command = "%s -np %s %s %s %s" % (
            self.launch_command, task_numcores, hosts_string, export_vars, task_command)

        return mpirun_rsh_command, None


# ==============================================================================
#
class LaunchMethodMPIRUNDPLACE(LaunchMethod):
    # TODO: This needs both mpirun and dplace

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)

        self.mpirun_command = None


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # dplace: job launcher for SGI systems (e.g. on Blacklight)
        self.launch_command = self._which('dplace')
        self.mpirun_command = self._which('mpirun')


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        dplace_offset = self._scheduler.slots2offset(task_slots)

        mpirun_dplace_command = "%s -np %d %s -c %d-%d %s" % \
            (self.mpirun_command, task_numcores, self.launch_command,
             dplace_offset, dplace_offset+task_numcores-1, task_command)

        return mpirun_dplace_command, None



# ==============================================================================
#
class LaunchMethodIBRUN(LaunchMethod):
    # NOTE: Don't think that with IBRUN it is possible to have
    # processes != cores ...

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # ibrun: wrapper for mpirun at TACC
        self.launch_command = self._which('ibrun')


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        ibrun_offset = self._scheduler.slots2offset(task_slots)

        ibrun_command = "%s -n %s -o %d %s" % \
                        (self.launch_command, task_numcores,
                         ibrun_offset, task_command)

        return ibrun_command, None



# ==============================================================================
#
class LaunchMethodPOE(LaunchMethod):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, scheduler):

        LaunchMethod.__init__(self, name, logger, scheduler)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # poe: LSF specific wrapper for MPI (e.g. yellowstone)
        self.launch_command = self._which('poe')


    # --------------------------------------------------------------------------
    #
    def construct_command(self, task_exec, task_args, task_numcores,
                          launch_script_hop, (task_slots)):

        # Count slots per host in provided slots description.
        hosts = {}
        for slot in task_slots:
            host = slot.split(':')[0]
            if host not in hosts:
                hosts[host] = 1
            else:
                hosts[host] += 1

        # Create string with format: "hostX N host
        hosts_string = ''
        for host in hosts:
            hosts_string += '%s %d ' % (host, hosts[host])

        if task_args:
            task_command = " ".join([task_exec, task_args])
        else:
            task_command = task_exec

        # Override the LSB_MCPU_HOSTS env variable as this is set by
        # default to the size of the whole pilot.
        poe_command = 'LSB_MCPU_HOSTS="%s" %s %s' % (
            hosts_string, self.launch_command, task_command)

        return poe_command, None



# ==============================================================================
#
# Base class for LRMS implementations.
#
# ==============================================================================
#
class LRMS(object):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, requested_cores):

        self.name            = name
        self._log            = logger
        self.requested_cores = requested_cores

        self._log.info("Configuring LRMS %s.", self.name)

        self.slot_list = []
        self.node_list = []
        self.cores_per_node = None

        self._configure()

        logger.info("Discovered execution environment: %s", self.node_list)

        # For now assume that all nodes have equal amount of cores
        cores_avail = len(self.node_list) * self.cores_per_node
        if cores_avail < int(requested_cores):
            raise Exception("Not enough cores available (%s) to satisfy allocation request (%s)." \
                            % (str(cores_avail), str(requested_cores)))


    # --------------------------------------------------------------------------
    #
    # This class-method creates the appropriate sub-class for the LRMS.
    #
    @classmethod
    def create(cls, name, requested_cores, logger):

        # TODO: Core counts dont have to be the same number for all hosts.

        # TODO: We might not have reserved the whole node.

        # TODO: Given that the Agent can determine the real core count, in
        #       principle we could just ignore the config and use as many as we
        #       have to our availability (taken into account that we might not
        #       have the full node reserved of course)
        #       Answer: at least on Yellowstone this doesnt work for MPI,
        #               as you can't spawn more tasks then the number of slots.

        # Make sure that we are the base-class!
        if cls != LRMS:
            raise Exception("LRMS Factory only available to base class!")

        try:
            implementation = {
                LRMS_NAME_FORK        : ForkLRMS,
                LRMS_NAME_LOADLEVELER : LoadLevelerLRMS,
                LRMS_NAME_LSF         : LSFLRMS,
                LRMS_NAME_PBSPRO      : PBSProLRMS,
                LRMS_NAME_SGE         : SGELRMS,
                LRMS_NAME_SLURM       : SLURMLRMS,
                LRMS_NAME_TORQUE      : TORQUELRMS
            }[name]
            return implementation(name, logger, requested_cores)
        except KeyError:
            raise Exception("LRMS type '%s' unknown!" % name)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        raise NotImplementedError("_Configure not implemented for LRMS type: %s." % self.name)



# ==============================================================================
#
class TORQUELRMS(LRMS):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, requested_cores):

        LRMS.__init__(self, name, logger, requested_cores)


    # --------------------------------------------------------------------------
    #
    def _configure(self):

        self._log.info("Configured to run on system with %s.", self.name)

        torque_nodefile = os.environ.get('PBS_NODEFILE')
        if torque_nodefile is None:
            msg = "$PBS_NODEFILE not set!"
            self._log.error(msg)
            raise Exception(msg)

        # Parse PBS the nodefile
        torque_nodes = [line.strip() for line in open(torque_nodefile)]
        self._log.info("Found Torque PBS_NODEFILE %s: %s", torque_nodefile, torque_nodes)

        # Number of cpus involved in allocation
        val = os.environ.get('PBS_NCPUS')
        if val:
            torque_num_cpus = int(val)
        else:
            msg = "$PBS_NCPUS not set! (new Torque version?)"
            torque_num_cpus = None
            self._log.warning(msg)

        # Number of nodes involved in allocation
        val = os.environ.get('PBS_NUM_NODES')
        if val:
            torque_num_nodes = int(val)
        else:
            msg = "$PBS_NUM_NODES not set! (old Torque version?)"
            torque_num_nodes = None
            self._log.warning(msg)

        # Number of cores (processors) per node
        val = os.environ.get('PBS_NUM_PPN')
        if val:
            torque_cores_per_node = int(val)
        else:
            msg = "$PBS_NUM_PPN is not set!"
            torque_cores_per_node = None
            self._log.warning(msg)

        if torque_cores_per_node in [None, 1]:
            # lets see if SAGA has been forthcoming with some information
            self._log.warning("fall back to $SAGA_PPN : %s", os.environ.get ('SAGA_PPN', None))
            torque_cores_per_node = int(os.environ.get('SAGA_PPN', torque_cores_per_node))

        # Number of entries in nodefile should be PBS_NUM_NODES * PBS_NUM_PPN
        torque_nodes_length = len(torque_nodes)
        torque_node_list    = list(set(torque_nodes))

      # if torque_num_nodes and torque_cores_per_node and \
      #     torque_nodes_length < torque_num_nodes * torque_cores_per_node:
      #     msg = "Number of entries in $PBS_NODEFILE (%s) does not match with $PBS_NUM_NODES*$PBS_NUM_PPN (%s*%s)" % \
      #           (torque_nodes_length, torque_num_nodes,  torque_cores_per_node)
      #     raise Exception(msg)

        # only unique node names
        torque_node_list_length = len(torque_node_list)
        self._log.debug("Node list: %s(%d)", torque_node_list, torque_node_list_length)

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


# ==============================================================================
#
class PBSProLRMS(LRMS):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, requested_cores):

        LRMS.__init__(self, name, logger, requested_cores)


    # --------------------------------------------------------------------------
    #
    def _configure(self):
        # TODO: $NCPUS?!?! = 1 on archer

        pbspro_nodefile = os.environ.get('PBS_NODEFILE')

        if pbspro_nodefile is None:
            msg = "$PBS_NODEFILE not set!"
            self._log.error(msg)
            raise Exception(msg)

        self._log.info("Found PBSPro $PBS_NODEFILE %s." % pbspro_nodefile)

        # Dont need to parse the content of nodefile for PBSPRO, only the length
        # is interesting, as there are only duplicate entries in it.
        pbspro_nodes_length = len([line.strip() for line in open(pbspro_nodefile)])

        # Number of Processors per Node
        val = os.environ.get('NUM_PPN')
        if val:
            pbspro_num_ppn = int(val)
        else:
            msg = "$NUM_PPN not set!"
            self._log.error(msg)
            raise Exception(msg)

        # Number of Nodes allocated
        val = os.environ.get('NODE_COUNT')
        if val:
            pbspro_node_count = int(val)
        else:
            msg = "$NODE_COUNT not set!"
            self._log.error(msg)
            raise Exception(msg)

        # Number of Parallel Environments
        val = os.environ.get('NUM_PES')
        if val:
            pbspro_num_pes = int(val)
        else:
            msg = "$NUM_PES not set!"
            self._log.error(msg)
            raise Exception(msg)

        pbspro_vnodes = self._parse_pbspro_vnodes()

        # Verify that $NUM_PES == $NODE_COUNT * $NUM_PPN == len($PBS_NODEFILE)
        if not (pbspro_node_count * pbspro_num_ppn == pbspro_num_pes == pbspro_nodes_length):
            self._log.warning("NUM_PES != NODE_COUNT * NUM_PPN != len($PBS_NODEFILE)")

        self.cores_per_node = pbspro_num_ppn
        self.node_list = pbspro_vnodes


    # --------------------------------------------------------------------------
    #
    def _parse_pbspro_vnodes(self):

        # PBS Job ID
        val = os.environ.get('PBS_JOBID')
        if val:
            pbspro_jobid = val
        else:
            msg = "$PBS_JOBID not set!"
            self._log.error(msg)
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
        rhs = vnodes_str.split('=',1)[1].strip()
        self._log.debug("input: %s", rhs)

        nodes_list = []
        # Break up the individual node partitions into vnode slices
        while True:
            idx = rhs.find(')+(')

            node_str = rhs[1:idx]
            nodes_list.append(node_str)
            rhs = rhs[idx+2:]

            if idx < 0:
                break

        vnodes_list = []
        cpus_list = []
        # Split out the slices into vnode name and cpu count
        for node_str in nodes_list:
            slices = node_str.split('+')
            for _slice in slices:
                vnode, cpus = _slice.split(':')
                cpus = int(cpus.split('=')[1])
                self._log.debug("vnode: %s cpus: %s", vnode, cpus)
                vnodes_list.append(vnode)
                cpus_list.append(cpus)

        self._log.debug("vnodes: %s", vnodes_list)
        self._log.debug("cpus: %s", cpus_list)

        cpus_list = list(set(cpus_list))
        min_cpus = int(min(cpus_list))

        if len(cpus_list) > 1:
            self._log.debug("Detected vnodes of different sizes: %s, the minimal is: %d.", cpus_list, min_cpus)

        node_list = []
        for vnode in vnodes_list:
            # strip the last _0 of the vnodes to get the node name
            node_list.append(vnode.rsplit('_', 1)[0])

        # only unique node names
        node_list = list(set(node_list))
        self._log.debug("Node list: %s", node_list)

        # Return the list of node names
        return node_list



# ==============================================================================
#
class SLURMLRMS(LRMS):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, requested_cores):

        LRMS.__init__(self, name, logger, requested_cores)


    # --------------------------------------------------------------------------
    #
    def _configure(self):

        slurm_nodelist = os.environ.get('SLURM_NODELIST')
        if slurm_nodelist is None:
            msg = "$SLURM_NODELIST not set!"
            self._log.error(msg)
            raise Exception(msg)

        # Parse SLURM nodefile environment variable
        slurm_nodes = hostlist.expand_hostlist(slurm_nodelist)
        self._log.info("Found SLURM_NODELIST %s. Expanded to: %s", slurm_nodelist, slurm_nodes)

        # $SLURM_NPROCS = Total number of cores allocated for the current job
        slurm_nprocs_str = os.environ.get('SLURM_NPROCS')
        if slurm_nprocs_str is None:
            msg = "$SLURM_NPROCS not set!"
            self._log.error(msg)
            raise Exception(msg)
        else:
            slurm_nprocs = int(slurm_nprocs_str)

        # $SLURM_NNODES = Total number of (partial) nodes in the job's resource allocation
        slurm_nnodes_str = os.environ.get('SLURM_NNODES')
        if slurm_nnodes_str is None:
            msg = "$SLURM_NNODES not set!"
            self._log.error(msg)
            raise Exception(msg)
        else:
            slurm_nnodes = int(slurm_nnodes_str)

        # $SLURM_CPUS_ON_NODE = Number of cores per node (physically)
        slurm_cpus_on_node_str = os.environ.get('SLURM_CPUS_ON_NODE')
        if slurm_cpus_on_node_str is None:
            msg = "$SLURM_CPUS_ON_NODE not set!"
            self._log.error(msg)
            raise Exception(msg)
        else:
            slurm_cpus_on_node = int(slurm_cpus_on_node_str)

        # Verify that $SLURM_NPROCS <= $SLURM_NNODES * $SLURM_CPUS_ON_NODE
        if not slurm_nprocs <= slurm_nnodes * slurm_cpus_on_node:
            self._log.warning("$SLURM_NPROCS(%d) <= $SLURM_NNODES(%d) * $SLURM_CPUS_ON_NODE(%d)",
                            slurm_nprocs, slurm_nnodes, slurm_cpus_on_node)

        # Verify that $SLURM_NNODES == len($SLURM_NODELIST)
        if slurm_nnodes != len(slurm_nodes):
            self._log.error("$SLURM_NNODES(%d) != len($SLURM_NODELIST)(%d)",
                           slurm_nnodes, len(slurm_nodes))

        # Report the physical number of cores or the total number of cores
        # in case of a single partial node allocation.
        self.cores_per_node = min(slurm_cpus_on_node, slurm_nprocs)

        self.node_list = slurm_nodes



# ==============================================================================
#
class SGELRMS(LRMS):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, requested_cores):

        LRMS.__init__(self, name, logger, requested_cores)


    # --------------------------------------------------------------------------
    #
    def _configure(self):

        sge_hostfile = os.environ.get('PE_HOSTFILE')
        if sge_hostfile is None:
            msg = "$PE_HOSTFILE not set!"
            self._log.error(msg)
            raise Exception(msg)

        # SGE core configuration might be different than what multiprocessing
        # announces
        # Alternative: "qconf -sq all.q|awk '/^slots *[0-9]+$/{print $2}'"

        # Parse SGE hostfile for nodes
        sge_node_list = [line.split()[0] for line in open(sge_hostfile)]
        # Keep only unique nodes
        sge_nodes = list(set(sge_node_list))
        self._log.info("Found PE_HOSTFILE %s. Expanded to: %s", sge_hostfile, sge_nodes)

        # Parse SGE hostfile for cores
        sge_cores_count_list = [int(line.split()[1]) for line in open(sge_hostfile)]
        sge_core_counts = list(set(sge_cores_count_list))
        sge_cores_per_node = min(sge_core_counts)
        self._log.info("Found unique core counts: %s Using: %d", sge_core_counts, sge_cores_per_node)

        self.node_list = sge_nodes
        self.cores_per_node = sge_cores_per_node



# ==============================================================================
#
class LSFLRMS(LRMS):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, requested_cores):

        LRMS.__init__(self, name, logger, requested_cores)


    # --------------------------------------------------------------------------
    #
    def _configure(self):

        lsf_hostfile = os.environ.get('LSB_DJOB_HOSTFILE')
        if lsf_hostfile is None:
            msg = "$LSB_DJOB_HOSTFILE not set!"
            self._log.error(msg)
            raise Exception(msg)

        lsb_mcpu_hosts = os.environ.get('LSB_MCPU_HOSTS')
        if lsb_mcpu_hosts is None:
            msg = "$LSB_MCPU_HOSTS not set!"
            self._log.error(msg)
            raise Exception(msg)

        # parse LSF hostfile
        # format:
        # <hostnameX>
        # <hostnameX>
        # <hostnameY>
        # <hostnameY>
        #
        # There are in total "-n" entries (number of tasks)
        # and "-R" entries per host (tasks per host).
        # (That results in "-n" / "-R" unique hosts)
        #
        lsf_nodes = [line.strip() for line in open(lsf_hostfile)]
        self._log.info("Found LSB_DJOB_HOSTFILE %s. Expanded to: %s",
                      lsf_hostfile, lsf_nodes)
        lsf_node_list = list(set(lsf_nodes))

        # Grab the core (slot) count from the environment
        # Format: hostX N hostY N hostZ N
        lsf_cores_count_list = map(int, lsb_mcpu_hosts.split()[1::2])
        lsf_core_counts = list(set(lsf_cores_count_list))
        lsf_cores_per_node = min(lsf_core_counts)
        self._log.info("Found unique core counts: %s Using: %d",
                      lsf_core_counts, lsf_cores_per_node)

        self.node_list = lsf_node_list
        self.cores_per_node = lsf_cores_per_node



# ==============================================================================
#
class LoadLevelerLRMS(LRMS):

    # --------------------------------------------------------------------------
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

    # --------------------------------------------------------------------------
    #
    # BG/Q Config
    #
    BGQ_CORES_PER_NODE      = 16
    BGQ_NODES_PER_BOARD     = 32 # NODE == Compute Card == Chip module
    BGQ_BOARDS_PER_MIDPLANE = 16 # NODE BOARD == NODE CARD
    BGQ_MIDPLANES_PER_RACK  = 2


    # --------------------------------------------------------------------------
    #
    # Default mapping = "ABCDE(T)"
    #
    # http://www.redbooks.ibm.com/redbooks/SG247948/wwhelp/wwhimpl/js/html/wwhelp.htm
    #
    BGQ_MAPPING = "ABCDE"


    # --------------------------------------------------------------------------
    #
    # Board labels (Rack, Midplane, Node)
    #
    BGQ_BOARD_LABELS = ['R', 'M', 'N']


    # --------------------------------------------------------------------------
    #
    # Dimensions of a (sub-)block
    #
    BGQ_DIMENSION_LABELS = ['A', 'B', 'C', 'D', 'E']


    # --------------------------------------------------------------------------
    #
    # Supported sub-block sizes (number of nodes).
    # This influences the effectiveness of mixed-size allocations
    # (and might even be a hard requirement from a topology standpoint).
    #
    # TODO: Do we actually need to restrict our sub-block sizes to this set?
    #
    BGQ_SUPPORTED_SUB_BLOCK_SIZES = [1, 2, 4, 8, 16, 32, 64, 128, 256, 512]


    # --------------------------------------------------------------------------
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


    # --------------------------------------------------------------------------
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

    # --------------------------------------------------------------------------
    #
    # Shape of whole BG/Q Midplane
    #
    BGQ_MIDPLANE_SHAPE = {'A': 4, 'B': 4, 'C': 4, 'D': 4, 'E': 2} # '4x4x4x4x2'


    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, requested_cores):

        self.torus_block            = None
        self.loadl_bg_block         = None
        self.shape_table            = None
        self.torus_dimension_labels = None

        LRMS.__init__(self, name, logger, requested_cores)

    # --------------------------------------------------------------------------
    #
    def _configure(self):

        # Determine method for determining hosts,
        # either through hostfile or BG/Q environment.
        loadl_hostfile = os.environ.get('LOADL_HOSTFILE')
        self.loadl_bg_block = os.environ.get('LOADL_BG_BLOCK')
        if loadl_hostfile is None and self.loadl_bg_block is None:
            msg = "Neither $LOADL_HOSTFILE or $LOADL_BG_BLOCK set!"
            self._log.error(msg)
            raise Exception(msg)

        # Determine the size of the pilot allocation
        if loadl_hostfile is not None:
            # Non Blue Gene Load Leveler installation.

            loadl_total_tasks_str = os.environ.get('LOADL_TOTAL_TASKS')
            if loadl_total_tasks_str is None:
                msg = "$LOADL_TOTAL_TASKS not set!"
                self._log.error(msg)
                raise Exception(msg)
            else:
                loadl_total_tasks = int(loadl_total_tasks_str)

            # Construct the host list
            loadl_nodes = [line.strip() for line in open(loadl_hostfile)]
            self._log.info("Found LOADL_HOSTFILE %s. Expanded to: %s",
                          loadl_hostfile, loadl_nodes)
            loadl_node_list = list(set(loadl_nodes))

            # Verify that $LLOAD_TOTAL_TASKS == len($LOADL_HOSTFILE)
            if loadl_total_tasks != len(loadl_nodes):
                self._log.error("$LLOAD_TOTAL_TASKS(%d) != len($LOADL_HOSTFILE)(%d)",
                               loadl_total_tasks, len(loadl_nodes))

            # Determine the number of cpus per node.  Assume:
            # cores_per_node = lenght(nodefile) / len(unique_nodes_in_nodefile)
            loadl_cpus_per_node = len(loadl_nodes) / len(loadl_node_list)

        elif self.loadl_bg_block is not None:
            # Blue Gene specific.

          # # FIXME: the setting below is unused?
          # #        So why are we raising an exception?
          # loadl_bg_size_str = os.environ.get('LOADL_BG_SIZE')
          # if loadl_bg_size_str is None:
          #     msg = "$LOADL_BG_SIZE not set!"
          #     self._log.error(msg)
          #     raise Exception(msg)
          # else:
          #     loadl_bg_size = int(loadl_bg_size_str)

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
                elif "BG Midplane List: " in line:
                    loadl_bg_midplane_list_str = line.split(':')[1].strip()
                elif "BG Shape Allocated: " in line:
                    loadl_bg_block_shape_str = line.split(':')[1].strip()
                elif "BG Size Allocated: " in line:
                    loadl_bg_block_size_str = line.split(':')[1].strip()
            if not loadl_bg_board_list_str:
                msg = "No board list found in llq output!"
                self._log.error(msg)
                raise Exception(msg)
            self._log.debug("BG Node Board List: %s" % loadl_bg_board_list_str)
            if not loadl_bg_midplane_list_str:
                msg = "No midplane list found in llq output!"
                self._log.error(msg)
                raise Exception(msg)
            self._log.debug("BG Midplane List: %s" % loadl_bg_midplane_list_str)
            if not loadl_bg_block_shape_str:
                msg = "No board shape found in llq output!"
                self._log.error(msg)
                raise Exception(msg)
            self._log.debug("BG Shape Allocated: %s" % loadl_bg_block_shape_str)
            if not loadl_bg_block_size_str:
                msg = "No board size found in llq output!"
                self._log.error(msg)
                raise Exception(msg)
            loadl_bg_block_size = int(loadl_bg_block_size_str)
            self._log.debug("BG Size Allocated: %d" % loadl_bg_block_size)

            # Build nodes data structure to be handled by Torus Scheduler
            try:
                self.torus_block = self._bgq_construct_block(
                    loadl_bg_block_shape_str, loadl_bg_board_list_str,
                    loadl_bg_block_size, loadl_bg_midplane_list_str)
            except Exception as e:
                msg = "Couldn't construct block: %s" % e.message
                self._log.error(msg)
                raise Exception(msg)
            self._log.debug("Torus block constructed:")
            for e in self.torus_block:
                self._log.debug("%s %s %s %s" %
                                (e[0], [e[1][key] for key in sorted(e[1])], e[2], e[3]))

            try:
                loadl_node_list = [entry[SchedulerTorus.TORUS_BLOCK_NAME] for entry in self.torus_block]
            except Exception as e:
                msg = "Couldn't construct node list."
                self._log.error(msg)
                raise Exception(msg)
            #self._log.debug("Node list constructed: %s" % loadl_node_list)

            # Construct sub-block table
            try:
                self.shape_table = self._bgq_create_sub_block_shape_table(loadl_bg_block_shape_str)
            except Exception as e:
                msg = "Couldn't construct shape table: %s" % e.message
                self._log.error(msg)
                raise Exception(msg)
            self._log.debug("Shape table constructed: ")
            for (size, dim) in [(key, self.shape_table[key]) for key in sorted(self.shape_table)]:
                self._log.debug("%s %s" % (size, [dim[key] for key in sorted(dim)]))

            # Determine the number of cpus per node
            loadl_cpus_per_node = self.BGQ_CORES_PER_NODE

            # BGQ Specific Torus labels
            self.torus_dimension_labels = self.BGQ_DIMENSION_LABELS

        self.node_list = loadl_node_list
        self.cores_per_node = loadl_cpus_per_node

        self._log.debug("Sleeping for #473 ...")
        time.sleep(5)
        self._log.debug("Configure done")


    # --------------------------------------------------------------------------
    #
    # Walk the block and return the node name for the given location
    #
    def _bgq_nodename_by_loc(self, midplanes, board, location):

        self._log.debug("Starting nodebyname - midplanes:%s, board:%d" % (midplanes, board))

        first = True
        node = self.BGQ_BLOCK_STARTING_CORNERS[board]

        # TODO: Does the order of walking matter?
        #       It might because of the starting blocks ...
        for dim in self.BGQ_DIMENSION_LABELS: # [::-1]:
            max_length = location[dim]
            self._log.debug("Within dim loop dim:%s, max_length: %d" % (dim, max_length))

            cur_length = 0
            # Loop while we are not at the final depth
            while cur_length < max_length:
                self._log.debug("beginning of while loop, cur_length: %d" % cur_length)

                if cur_length % 2 == 0:
                    # Stay within the board
                    node = self.BGQ_BOARD_TOPO[node][dim]

                else:
                    # We jump to another board.
                    self._log.debug("jumping to new board from board: %d, dim: %s)" % (board, dim))
                    board = self.BGQ_MIDPLANE_TOPO[board][dim]
                    self._log.debug("board is now: %d" % board)

                    # If we switch boards in the B dimension,
                    # we seem to "land" at the opposite E dimension.
                    if dim  == 'B':
                        node = self.BGQ_BOARD_TOPO[node]['E']

                self._log.debug("node is now: %d" % node)

                # Increase the length for the next iteration
                cur_length += 1

            self._log.debug("Wrapping inside dim loop dim:%s" % (dim))

        # TODO: This will work for midplane expansion in one dimension only
        midplane_idx = max(location.values()) / 4
        rack = midplanes[midplane_idx]['R']
        midplane = midplanes[midplane_idx]['M']

        nodename = 'R%.2d-M%.1d-N%.2d-J%.2d' % (rack, midplane, board, node)
        self._log.debug("from location %s constructed node name: %s, left at board: %d" % (self.loc2str(location), nodename, board))

        return nodename


    # --------------------------------------------------------------------------
    #
    # Convert the board string as given by llq into a board structure
    #
    # E.g. 'R00-M1-N08,R00-M1-N09,R00-M1-N10,R00-M0-N11' =>
    # [{'R': 0, 'M': 1, 'N': 8}, {'R': 0, 'M': 1, 'N': 9},
    #  {'R': 0, 'M': 1, 'N': 10}, {'R': 0, 'M': 0, 'N': 11}]
    #
    def _bgq_str2boards(self, boards_str):

        boards = boards_str.split(',')

        board_dict_list = []

        for board in boards:
            elements = board.split('-')

            board_dict = {}
            for l, e in zip(self.BGQ_BOARD_LABELS, elements):
                board_dict[l] = int(e.split(l)[1])

            board_dict_list.append(board_dict)

        return board_dict_list


    # --------------------------------------------------------------------------
    #
    # Convert the midplane string as given by llq into a midplane structure
    #
    # E.g. 'R04-M0,R04-M1' =>
    # [{'R': 4, 'M': 0}, {'R': 4, 'M': 1}]
    #
    #
    def _bgq_str2midplanes(self, midplane_str):

        midplanes = midplane_str.split(',')

        midplane_dict_list = []
        for midplane in midplanes:
            elements = midplane.split('-')

            midplane_dict = {}
            # Take the first two labels
            for l, e in zip(self.BGQ_BOARD_LABELS[:2], elements):
                midplane_dict[l] = int(e.split(l)[1])

            midplane_dict_list.append(midplane_dict)

        return midplane_dict_list


    # --------------------------------------------------------------------------
    #
    # Convert the string as given by llq into a block shape structure:
    #
    # E.g. '1x2x3x4x5' => {'A': 1, 'B': 2, 'C': 3, 'D': 4, 'E': 5}
    #
    def _bgq_str2shape(self, shape_str):

        # Get the lengths of the shape
        shape_lengths = shape_str.split('x', 4)

        shape_dict = {}
        for dim, length in zip(self.BGQ_DIMENSION_LABELS, shape_lengths):
            shape_dict[dim] = int(length)

        return shape_dict


    # --------------------------------------------------------------------------
    #
    # Multiply two shapes
    #
    def _multiply_shapes(self, shape1, shape2):

        result = {}

        for dim in self.BGQ_DIMENSION_LABELS:
            try:
                val1 = shape1[dim]
            except KeyError:
                val1 = 1

            try:
                val2 = shape2[dim]
            except KeyError:
                val2 = 1

            result[dim] = val1 * val2

        return result


    # --------------------------------------------------------------------------
    #
    # Convert location dict into a tuple string
    # E.g. {'A': 1, 'C': 4, 'B': 1, 'E': 2, 'D': 4} => '(1,4,1,2,4)'
    #
    def loc2str(self, loc):
        return str(tuple(loc[dim] for dim in self.BGQ_DIMENSION_LABELS))


    # --------------------------------------------------------------------------
    #
    # Convert a shape dict into string format
    #
    # E.g. {'A': 1, 'C': 4, 'B': 1, 'E': 2, 'D': 4} => '1x4x1x2x4'
    #
    def shape2str(self, shape):

        shape_str = ''
        for l in self.BGQ_DIMENSION_LABELS:

            # Get the corresponding count
            shape_str += str(shape[l])

            # Add an 'x' behind all but the last label
            if l in self.BGQ_DIMENSION_LABELS[:-1]:
                shape_str += 'x'

        return shape_str


    # --------------------------------------------------------------------------
    #
    # Return list of nodes that make up the block
    #
    # Format: [(index, location, nodename, status), (i, c, n, s), ...]
    #
    # TODO: This function and _bgq_nodename_by_loc should be changed so that we
    #       only walk the torus once?
    #
    def _bgq_get_block(self, midplanes, board, shape):

        self._log.debug("Shape: %s", shape)

        nodes = []
        index = 0

        for a in range(shape['A']):
            for b in range(shape['B']):
                for c in range(shape['C']):
                    for d in range(shape['D']):
                        for e in range(shape['E']):
                            location = {'A': a, 'B': b, 'C': c, 'D': d, 'E': e}
                            nodename = self._bgq_nodename_by_loc(midplanes, board, location)
                            nodes.append([index, location, nodename, FREE])
                            index += 1

        return nodes


    # --------------------------------------------------------------------------
    #
    # Use block shape and board list to construct block structure
    #
    # The 5 dimensions are denoted by the letters A, B, C, D, and E, T for the core (0-15).
    # The latest dimension E is always 2, and is contained entirely within a midplane.
    # For any compute block, compute nodes (as well midplanes for large blocks) are combined in 4 dimensions,
    # only 4 dimensions need to be considered.
    #
    #  128 nodes: BG Shape Allocated: 2x2x4x4x2
    #  256 nodes: BG Shape Allocated: 4x2x4x4x2
    #  512 nodes: BG Shape Allocated: 1x1x1x1
    #  1024 nodes: BG Shape Allocated: 1x1x1x2
    #
    def _bgq_construct_block(self, block_shape_str, boards_str,
                            block_size, midplane_list_str):

        llq_shape = self._bgq_str2shape(block_shape_str)

        # TODO: Could check this, but currently _shape2num is part of the other class
        #if self._shape2num_nodes(llq_shape) != block_size:
        #    self._log.error("Block Size doesn't match Block Shape")

        # If the block is equal to or greater than a Midplane,
        # then there is no board list provided.
        # But because at that size, we have only full midplanes,
        # we can construct it.

        if block_size >= 1024:
            #raise NotImplementedError("Currently multiple midplanes are not yet supported.")

            # BG Size: 1024, BG Shape: 1x1x1x2, BG Midplane List: R04-M0,R04-M1
            midplanes = self._bgq_str2midplanes(midplane_list_str)

            # Start of at the "lowest" available rack/midplane/board
            # TODO: No other explanation than that this seems to be the convention?
            # TODO: Can we safely assume that they are sorted?
            #rack = midplane_dict_list[0]['R']
            #midplane = midplane_dict_list[0]['M']
            board = 0

            # block_shape = llq_shape * BGQ_MIDPLANE_SHAPE
            block_shape = self._multiply_shapes(self.BGQ_MIDPLANE_SHAPE, llq_shape)
            self._log.debug("Resulting shape after multiply: %s" % block_shape)

        elif block_size == 512:
            # Full midplane

            # BG Size: 1024, BG Shape: 1x1x1x2, BG Midplane List: R04-M0,R04-M1
            midplanes = self._bgq_str2midplanes(midplane_list_str)

            # Start of at the "lowest" available rack/midplane/board
            # TODO: No other explanation than that this seems to be the convention?
            #rack = midplane_dict_list[0]['R'] # Assume they are all equal
            #midplane = min([entry['M'] for entry in midplane_dict_list])
            board = 0

            block_shape = self.BGQ_MIDPLANE_SHAPE

        else:
            # Within single midplane, < 512 nodes

            board_dict_list = self._bgq_str2boards(boards_str)
            self._log.debug("Board dict list:\n%s", '\n'.join([str(x) for x in board_dict_list]))

            midplanes = [{'R': board_dict_list[0]['R'],
                          'M': board_dict_list[0]['M']}]

            # Start of at the "lowest" available board.
            # TODO: No other explanation than that this seems to be the convention?
            board = min([entry['N'] for entry in board_dict_list])

            block_shape = llq_shape

        # From here its all equal (assuming our walker does the walk and not just the talk!)
        block = self._bgq_get_block(midplanes, board, block_shape)

        # TODO: Check returned block:
        #       - Length
        #       - No duplicates

        return block


    # --------------------------------------------------------------------------
    #
    # Construction of sub-block shapes based on overall block allocation.
    #
    # Depending on the size of the total allocated block, the maximum size
    # of a subblock can be 512 nodes.
    #
    #
    def _bgq_create_sub_block_shape_table(self, shape_str):

        # Convert the shape string into dict structure
        #
        # For < 512 nodes: the dimensions within a midplane (AxBxCxDxE)
        # For >= 512 nodes: the dimensions between the midplanes (AxBxCxD)
        #
        if len(shape_str.split('x')) == 5:
            block_shape = self._bgq_str2shape(shape_str)
        elif len(shape_str.split('x')) == 4:
            block_shape = self.BGQ_MIDPLANE_SHAPE
        else:
            raise Exception('Invalid shape string: %s' % shape_str)

        # Dict to store the results
        table = {}

        # Create a sub-block dict with shape 1x1x1x1x1
        sub_block_shape = {}
        for l in self.BGQ_DIMENSION_LABELS:
            sub_block_shape[l] = 1

        # Look over all the dimensions starting at the most right
        for dim in self.BGQ_MAPPING[::-1]:
            while True:

                # Calculate the number of nodes for the current shape
                num_nodes = reduce(mul, filter(lambda length: length != 0, sub_block_shape.values()))

                if num_nodes in self.BGQ_SUPPORTED_SUB_BLOCK_SIZES:
                    table[num_nodes] = copy.copy(sub_block_shape)
                else:
                    self._log.warning("Non supported sub-block size: %d.", num_nodes)

                # Done with iterating this dimension
                if sub_block_shape[dim] >= block_shape[dim]:
                    break

                # Increase the length in this dimension for the next iteration.
                if sub_block_shape[dim] == 1:
                    sub_block_shape[dim] = 2
                elif sub_block_shape[dim] == 2:
                    sub_block_shape[dim] = 4

        return table



# ==============================================================================
#
class ForkLRMS(LRMS):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, requested_cores):

        LRMS.__init__(self, name, logger, requested_cores)


    # --------------------------------------------------------------------------
    #
    def _configure(self):

        self._log.info("Using fork on localhost.")

        detected_cpus = multiprocessing.cpu_count()

        if profile_agent :
            # when we profile the agent, we fake any number of CUs...
            selected_cpus = self.requested_cores
        else :
            selected_cpus = max(detected_cpus, self.requested_cores)


        self._log.info("Detected %d cores on localhost, using %d.", detected_cpus, selected_cpus)

        self.node_list = ["localhost"]
        self.cores_per_node = selected_cpus



# ==============================================================================
#
# Worker Classes
#
# ==============================================================================
#
class ExecWorker(COMPONENT_TYPE):
    """
    Manage the creation of CU processes, and watch them until they are completed
    (one way or the other).  The spawner thus moves the unit from
    PendingExecution to Executing, and then to a final state (or PendingStageOut
    of course).
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, agent, lrms, scheduler,
                 task_launcher, mpi_launcher, command_queue,
                 execution_queue, update_queue, stageout_queue,
                 pilot_id, session_id):

        prof('ExecWorker init')

        COMPONENT_TYPE.__init__(self)
        self._terminate = COMPONENT_MODE.Event()

        self.name              = name
        self._log              = logger
        self._agent            = agent
        self._lrms             = lrms
        self._scheduler        = scheduler
        self._task_launcher    = task_launcher
        self._mpi_launcher     = mpi_launcher
        self._command_queue    = command_queue
        self._execution_queue  = execution_queue
        self._stageout_queue   = stageout_queue
        self._update_queue     = update_queue
        self._pilot_id         = pilot_id
        self._session_id       = session_id

        self.configure ()


    # --------------------------------------------------------------------------
    #
    # This class-method creates the appropriate sub-class for the Launch Method.
    #
    @classmethod
    def create(cls, name, spawner, logger, agent, lrms, scheduler,
               task_launcher, mpi_launcher, command_queue, 
               execution_queue, update_queue, stageout_queue,
               pilot_id, session_id):

        # Make sure that we are the base-class!
        if cls != ExecWorker:
            raise Exception("ExecWorker Factory only available to base class!")

        try:
            implementation = {
                SPAWNER_NAME_POPEN : ExecWorker_POPEN,
                SPAWNER_NAME_SHELL : ExecWorker_SHELL
            }[spawner]

            impl = implementation(name, logger, agent, lrms, scheduler,
                                  task_launcher, mpi_launcher, command_queue, 
                                  execution_queue, update_queue, stageout_queue,
                                  pilot_id, session_id)
            impl.start ()
            return impl

        except KeyError:
            raise Exception("ExecWorker '%s' unknown!" % name)


    # --------------------------------------------------------------------------
    #
    def __del__ (self):
        self.close ()


    # --------------------------------------------------------------------------
    #
    def stop(self):
        self._terminate.set()


    # --------------------------------------------------------------------------
    #
    def configure(self):
        # hook for initialization
        pass


    # --------------------------------------------------------------------------
    #
    def close(self):
        # hook for shutdown
        pass


    # --------------------------------------------------------------------------
    #
    def spawn(self, launcher, cu):
        raise NotImplementedError("spawn() not implemented for ExecWorker '%s'." % self.name)



# ==============================================================================
#
class ExecWorker_POPEN (ExecWorker) :

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, agent, lrms, scheduler,
                 task_launcher, mpi_launcher, spawner, command_queue, 
                 execution_queue, update_queue,
                 pilot_id, session_id):

        prof('ExecWorker init')

        self._cus_to_watch   = list()
        self._cus_to_cancel  = list()
        self._watch_queue    = QUEUE_TYPE ()
        self._cu_environment = self._populate_cu_environment()


        ExecWorker.__init__ (self, name, logger, agent, lrms, scheduler,
                 task_launcher, mpi_launcher, spawner, command_queue, 
                 execution_queue, update_queue,
                 pilot_id, session_id)


        # run watcher thread
        watcher_name  = self.name.replace ('ExecWorker', 'ExecWatcher')
        self._watcher = threading.Thread(target = self._watch, 
                                         name   = watcher_name)
        self._watcher.start ()


    # --------------------------------------------------------------------------
    #
    def close(self):

        # shut down the watcher thread
        self._terminate.set()
        self._watcher.join()


    # --------------------------------------------------------------------------
    #
    def _populate_cu_environment(self):
        """Derive the environment for the cu's from our own environment."""

        # Get the environment of the agent
        new_env = copy.deepcopy(os.environ)

        #
        # Mimic what virtualenv's "deactivate" would do
        #
        old_path = new_env.pop('_OLD_VIRTUAL_PATH', None)
        if old_path:
            new_env['PATH'] = old_path

        old_home = new_env.pop('_OLD_VIRTUAL_PYTHONHOME', None)
        if old_home:
            new_env['PYTHON_HOME'] = old_home

        old_ps = new_env.pop('_OLD_VIRTUAL_PS1', None)
        if old_ps:
            new_env['PS1'] = old_ps

        new_env.pop('VIRTUAL_ENV', None)

        return new_env


    # --------------------------------------------------------------------------
    #
    def run(self):

        self._log.info("started %s.", self)

        try:
            # report initial slot status
            # TODO: Where does this abstraction belong?  Scheduler!
            self._log.debug(self._scheduler.slot_status())

            while not self._terminate.is_set():

                prof('ExecWorker pull cu from queue')
                cu = self._execution_queue.get()

                if not cu :
                    # 'None' is the wakeup signal
                    continue

                prof('ExecWorker got  cu from queue', uid=cu['_id'], tag='preprocess')


                try:

                    if cu['description']['mpi']:
                        launcher = self._mpi_launcher
                    else :
                        launcher = self._task_launcher

                    if not launcher:
                        self._agent.update_unit_state(
                                uid    = cu['_id'],
                                state  = rp.FAILED,
                                msg    = "no launcher (mpi=%s)" % cu['description']['mpi'],
                                logger = self._log.error)

                    self._log.debug("Launching unit with %s (%s).", launcher.name, launcher.launch_command)

                    assert(cu['opaque_slot']) # FIXME: no assert, but check
                    prof('ExecWorker unit launch', uid=cu['_id'])

                    # Start a new subprocess to launch the unit
                    # TODO: This is scheduler specific
                    self.spawn(launcher = launcher, cu = cu)


                except Exception as e:
                    # append the startup error to the units stderr.  This is
                    # not completely correct (as this text is not produced
                    # by the unit), but it seems the most intuitive way to
                    # communicate that error to the application/user.
                    cu['stderr'] += "\nPilot cannot start compute unit:\n%s\n%s" \
                                    % (str(e), traceback.format_exc())
                    cu['state']   = rp.FAILED
                    cu['stderr'] += "\nPilot cannot start compute unit: '%s'" % e

                    # Free the Slots, Flee the Flots, Ree the Frots!
                    if cu['opaque_slot']:
                        self._scheduler.unschedule(cu)

                    self._agent.update_unit_state(uid    = cu['_id'],
                                                  state  = rp.FAILED,
                                                  msg    = "unit execution failed",
                                                  logger = self._log.exception)


        except Exception as e:
            self._log.exception("Error in ExecWorker loop (%s)" % e)
            return

    # --------------------------------------------------------------------------
    #
    def spawn(self, launcher, cu):

        prof('ExecWorker spawn', uid=cu['_id'])

        launch_script_name = '%s/radical_pilot_cu_launch_script.sh' % cu['workdir']
        self._log.debug("Created launch_script: %s", launch_script_name)

        with open(launch_script_name, "w") as launch_script:
            launch_script.write('#!/bin/bash -l\n')
            launch_script.write('\n# Change to working directory for unit\ncd %s\n' % cu['workdir'])

            # Before the Big Bang there was nothing
            if cu['description']['pre_exec']:
                pre_exec_string = ''
                if isinstance(cu['description']['pre_exec'], list):
                    for elem in cu['description']['pre_exec']:
                        pre_exec_string += "%s\n" % elem
                else:
                    pre_exec_string += "%s\n" % cu['description']['pre_exec']
                launch_script.write('# Pre-exec commands\n%s' % pre_exec_string)

            # Create string for environment variable setting
            if cu['description']['environment'] and    \
                cu['description']['environment'].keys():
                env_string = 'export'
                for key,val in cu['description']['environment'].iteritems():
                    env_string += ' %s=%s' % (key, val)
                launch_script.write('# Environment variables\n%s\n' % env_string)

            # unit Arguments (if any)
            task_args_string = ''
            if cu['description']['arguments']:
                for arg in cu['description']['arguments']:
                    if not arg:
                        # ignore empty args
                        continue

                    arg = arg.replace('"', '\\"')          # Escape all double quotes
                    if arg[0] == arg[-1] == "'" :          # If a string is between outer single quotes,
                        task_args_string += '%s ' % arg    # ... pass it as is.
                    else:
                        task_args_string += '"%s" ' % arg  # Otherwise return between double quotes.

            launch_script_hop = "/usr/bin/env RP_SPAWNER_HOP=TRUE %s" % launch_script_name

            # The actual command line, constructed per launch-method
            prof('_Process construct command', uid=cu['_id'])
            try:
                launch_command, hop_cmd = \
                    launcher.construct_command(cu['description']['executable'],
                                               task_args_string,
                                               cu['description']['cores'],
                                               launch_script_hop,
                                               cu['opaque_slot'])
                if hop_cmd : cmdline = hop_cmd
                else       : cmdline = launch_script_name

            except Exception as e:
                msg = "Error in spawner (%s)" % e
                self._log.exception(msg)
                raise Exception(msg)

            launch_script.write('# The command to run\n%s\n' % launch_command)

            # After the universe dies the infrared death, there will be nothing
            if cu['description']['post_exec']:
                post_exec_string = ''
                if isinstance(cu['description']['post_exec'], list):
                    for elem in cu['description']['post_exec']:
                        post_exec_string += "%s\n" % elem
                else:
                    post_exec_string += "%s\n" % cu['description']['post_exec']
                launch_script.write('%s\n' % post_exec_string)

        # done writing to launch script, get it ready for execution.
        st = os.stat(launch_script_name)
        os.chmod(launch_script_name, st.st_mode | stat.S_IEXEC)

        _stdout_file_h = open(cu['stdout_file'], "w")
        _stderr_file_h = open(cu['stderr_file'], "w")

        self._log.info("Launching unit %s via %s in %s", cu['_id'], cmdline, cu['workdir'])
        prof('spawning pass to popen', uid=cu['_id'], tag='unit spawning')

        proc = subprocess.Popen(args               = cmdline,
                                bufsize            = 0,
                                executable         = None,
                                stdin              = None,
                                stdout             = _stdout_file_h,
                                stderr             = _stderr_file_h,
                                preexec_fn         = None,
                                close_fds          = True,
                                shell              = True,
                                cwd                = cu['workdir'],
                                env                = self._cu_environment,
                                universal_newlines = False,
                                startupinfo        = None,
                                creationflags      = 0)

        prof('spawning passed to popen', uid=cu['_id'], tag='unit spawning')

        cu['started'] = timestamp()
        cu['state']   = rp.EXECUTING
        cu['proc']    = proc

        # register for state update and watching
        self._agent.update_unit_state(uid    = cu['_id'],
                                      state  = rp.EXECUTING,
                                      msg    = "unit execution start")

        cu_list = blowup (cu, WATCH) 
        for _cu in cu_list :
            prof('push', msg="toward watching", uid=_cu['_id'], tag='task_launching')
            self._watch_queue.put(_cu)


    # --------------------------------------------------------------------------
    #
    def _watch(self):

        self._log.info("started %s.", self)

        try:

            while not self._terminate.is_set():

                cus = list()

                # See if there are cancel requests, or new units to watch
                try:
                    command = self._command_queue.get_nowait()

                    if command[COMMAND_TYPE] == COMMAND_CANCEL_COMPUTE_UNIT:
                        self._cus_to_cancel.append(command[COMMAND_ARG])
                    else:
                        raise Exception("Command %s not applicable in this context." %
                                        command[COMMAND_TYPE])

                except Queue.Empty:
                    # do nothing if we don't have any queued commands
                    pass


                try:

                    # we don't want to only wait for one CU -- then we would
                    # pull CU state too frequently.  OTOH, we also don't want to
                    # learn about CUs until all slots are filled, because then
                    # we may not be able to catch finishing CUs in time -- so
                    # there is a fine balance here.  Balance means 100 (FIXME).
                  # prof('ExecWorker popen watcher pull cu from queue')
                    MAX_QUEUE_BULKSIZE = 100
                    while len(cus) < MAX_QUEUE_BULKSIZE :
                        cus.append (self._watch_queue.get_nowait())

                except Queue.Empty:

                    # nothing found -- no problem, see if any CUs finshed
                    pass


                # add all cus we found to the watchlist
                for cu in cus :
                    prof('ExecWorker popen watcher got  cu from queue', uid=cu['_id'], tag='watching')
                    self._cus_to_watch.append (cu)

                # check on the known cus.
                action = self._check_running()

                if not action and not cus :
                    # nothing happend at all!  Zzz for a bit.
                    time.sleep(QUEUE_POLL_SLEEPTIME)


        except Exception as e:
            self._log.exception("Error in ExecWorker watch loop (%s)" % e)
            return


    # --------------------------------------------------------------------------
    # Iterate over all running tasks, check their status, and decide on the
    # next step.  Also check for a requested cancellation for the tasks.
    def _check_running(self):

        action = 0

        for cu in self._cus_to_watch:

            # poll subprocess object
            exit_code = cu['proc'].poll()
            now       = timestamp()

            if exit_code is None:
                # Process is still running

                if cu['_id'] in self._cus_to_cancel:

                    # FIXME: there is a race condition between the state poll
                    # above and the kill command below.  We probably should pull
                    # state after kill again?

                    # We got a request to cancel this cu
                    action += 1
                    cu['proc'].kill()
                    self._cus_to_cancel.remove(cu['_id'])
                    self._scheduler.unschedule(cu)

                    self._agent.update_unit_state(uid    = cu['_id'],
                                                  state  = rp.CANCELED,
                                                  msg    = "unit execution canceled")
                    prof('final', msg="execution canceled", uid=cu['_id'], tag='watching')
                    # NOTE: this is final, cu will not be touched anymore
                    cu = None

            else : 
                # we have a valid return code -- unit is final
                action += 1
                self._log.info("Unit %s has return code %s.", cu['_id'], exit_code)

                cu['exit_code'] = exit_code
                cu['finished']  = now

                # Free the Slots, Flee the Flots, Ree the Frots!
                self._scheduler.unschedule(cu)
                self._cus_to_watch.remove(cu)

                if exit_code != 0:

                    # The unit failed, no need to deal with its output data.
                    self._agent.update_unit_state(uid    = cu['_id'],
                                                  state  = rp.FAILED,
                                                  msg    = "unit execution failed")
                    prof('final', msg="execution failed", uid=cu['_id'], tag='watching')
                    # NOTE: this is final, cu will not be touched anymore
                    cu = None

                else:
                    # The unit finished cleanly, see if we need to deal with
                    # output data.  We always move to stageout, even if there are no
                    # directives -- at the very least, we'll upload stdout/stderr

                    self._agent.update_unit_state(uid    = cu['_id'],
                                                  state  = rp.STAGING_OUTPUT,
                                                  msg    = "unit execution completed")
                    cu_list = blowup (cu, STAGEOUT) 
                    for _cu in cu_list :
                        prof('push', msg="toward stageout", uid=_cu['_id'], tag='watching')
                        self._stageout_queue.put(_cu)

        return action


# ==============================================================================
#
class ExecWorker_SHELL(ExecWorker):


    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, agent, lrms, scheduler,
                 task_launcher, mpi_launcher, spawner, command_queue, 
                 execution_queue, update_queue,
                 pilot_id, session_id):

        ExecWorker.__init__ (self, name, logger, agent, lrms, scheduler,
                 task_launcher, mpi_launcher, spawner, command_queue, 
                 execution_queue, update_queue,
                 pilot_id, session_id)


    # --------------------------------------------------------------------------
    #
    def run(self):

        self._log.info("started %s.", self)

        # Mimic what virtualenv's "deactivate" would do
        self._deactivate = "# deactivate pilot virtualenv\n"

        old_path = os.environ.get('_OLD_VIRTUAL_PATH',       None)
        old_home = os.environ.get('_OLD_VIRTUAL_PYTHONHOME', None)
        old_ps1  = os.environ.get('_OLD_VIRTUAL_PS1',        None)

        if old_path: self._deactivate += 'export PATH="%s"\n'        % old_path 
        if old_home: self._deactivate += 'export PYTHON_HOME="%s"\n' % old_home 
        if old_ps1:  self._deactivate += 'export PS1="%s"\n'         % old_ps1

        self._deactivate += 'unset VIRTUAL_ENV\n\n'

        # the registry keeps track of units to watch, indexed by their shell
        # spawner process ID.  As the registry is shared between the spawner and
        # watcher thread, we use a lock while accessing it.
        self._registry      = dict()
        self._registry_lock = threading.RLock()

        self._cached_events = list() # keep monitoring events for pid's which
                                     # are not yet known

        # get some threads going -- those will do all the work.
        import saga.utils.pty_shell as sups
        self.launcher_shell = sups.PTYShell ("fork://localhost/")
        self.monitor_shell  = sups.PTYShell ("fork://localhost/")

        self.workdir = "%s/spawner.%s" % (os.getcwd(), self.name)


        ret, out, _  = self.launcher_shell.run_sync \
                           ("/bin/sh %s/agent/radical-pilot-spawner.sh %s" \
                           % (os.path.dirname (rp.__file__), self.workdir))
        if  ret != 0 :
            raise RuntimeError ("failed to bootstrap launcher: (%s)(%s)", ret, out)

        ret, out, _  = self.monitor_shell.run_sync \
                           ("/bin/sh %s/agent/radical-pilot-spawner.sh %s" \
                           % (os.path.dirname (rp.__file__), self.workdir))
        if  ret != 0 :
            raise RuntimeError ("failed to bootstrap monitor: (%s)(%s)", ret, out)

        # run watcher thread
        watcher_name  = self.name.replace ('ExecWorker', 'ExecWatcher')
        self._watcher = threading.Thread(target = self._watch, 
                                         name   = watcher_name)
        self._watcher.start ()



        try:
            # report initial slot status
            # TODO: Where does this abstraction belong?  Scheduler!
            self._log.debug(self._scheduler.slot_status())

            while not self._terminate.is_set():

                prof('ExecWorker pull cu from queue')
                cu = self._execution_queue.get()

                if not cu :
                    # 'None' is the wakeup signal
                    continue

                prof('ExecWorker got  cu from queue', uid=cu['_id'], tag='preprocess')


                try:

                    if cu['description']['mpi']:
                        launcher = self._mpi_launcher
                    else :
                        launcher = self._task_launcher

                    if not launcher:
                        self._agent.update_unit_state(
                                uid    = cu['_id'],
                                state  = rp.FAILED,
                                msg    = "no launcher (mpi=%s)" % cu['description']['mpi'],
                                logger = self._log.error)

                    self._log.debug("Launching unit with %s (%s).", launcher.name, launcher.launch_command)

                    assert(cu['opaque_slot']) # FIXME: no assert, but check
                    prof('ExecWorker unit launch', uid=cu['_id'])

                    # Start a new subprocess to launch the unit
                    # TODO: This is scheduler specific
                    self.spawn(launcher = launcher, cu = cu)


                except Exception as e:
                    # append the startup error to the units stderr.  This is
                    # not completely correct (as this text is not produced
                    # by the unit), but it seems the most intuitive way to
                    # communicate that error to the application/user.
                    cu['stderr'] += "\nPilot cannot start compute unit:\n%s\n%s" \
                                    % (str(e), traceback.format_exc())
                    cu['state']   = rp.FAILED
                    cu['stderr'] += "\nPilot cannot start compute unit: '%s'" % e

                    # Free the Slots, Flee the Flots, Ree the Frots!
                    if cu['opaque_slot']:
                        self._scheduler.unschedule(cu)

                    self._agent.update_unit_state(uid    = cu['_id'],
                                                  state  = rp.FAILED,
                                                  msg    = "unit execution failed",
                                                  logger = self._log.exception)


        except Exception as e:
            self._log.exception("Error in ExecWorker loop (%s)" % e)
            return

    # --------------------------------------------------------------------------
    #
    def _cu_to_cmd (self, cu, launcher) :

        # ----------------------------------------------------------------------
        def quote_args (args) :

            ret = list()
            for arg in args :

                # if string is between outer single quotes,
                #    pass it as is.
                # if string is between outer double quotes,
                #    pass it as is.
                # otherwise (if string is not quoted)
                #    escape all double quotes

                if  arg[0] == arg[-1]  == "'" :
                    ret.append (arg)
                elif arg[0] == arg[-1] == '"' :
                    ret.append (arg)
                else :
                    arg = arg.replace ('"', '\\"')
                    ret.append ('"%s"' % arg)

            return  ret
        # ----------------------------------------------------------------------

        args = ""
        env  = self._deactivate
        cwd  = ""
        pre  = ""
        post = ""
        io   = ""
        cmd  = ""

        descr = cu['description']

        if  cu['workdir'] :
            cwd += "# CU workdir\n"
            cwd += "mkdir -p %s\n" % cu['workdir']
            cwd += "cd       %s\n" % cu['workdir']
            cwd += "\n"

        if  descr['environment'] :
            env += "# CU environment\n"
            for e in descr['environment'] :
                env += "export %s=%s\n"  %  (e, descr['environment'][e])
            env += "\n"

        if  descr['pre_exec'] : 
            pre += "# CU pre-exec\n"
            pre += '\n'.join(descr['pre_exec' ])
            pre += "\n\n"

        if  descr['post_exec'] : 
            post += "# CU post-exec\n"
            post += '\n'.join(descr['post_exec' ])
            post += "\n\n"

        if  descr['arguments']  : 
            args  = ' ' .join (quote_args (descr['arguments']))

      # if  descr['stdin']  : io  += "<%s "  % descr['stdin']
      # else                : io  += "<%s "  % '/dev/null'
        if  descr['stdout'] : io  += "1>%s " % descr['stdout']
        else                : io  += "1>%s " %       'STDOUT'
        if  descr['stderr'] : io  += "2>%s " % descr['stderr']
        else                : io  += "2>%s " %       'STDERR'

        cmd, hop_cmd  = launcher.construct_command(descr['executable'], args,
                                                   descr['cores'], 
                                                   '/usr/bin/env RP_SPAWNER_HOP=TRUE "$0"',
                                                   cu['opaque_slot'])


        script = ""
        if hop_cmd :
            # the script will itself contain a remote callout which calls again
            # the script for the invokation of the real workload (cmd) -- we
            # thus introduce a guard for the first execution.  The hop_cmd MUST
            # set RP_SPAWNER_HOP to some value for the startup to work

            script += "# ------------------------------------------------------\n"
            script += '# perform one hop for the actual command launch\n'
            script += 'if test -z "$RP_SPAWNER_HOP"\n'
            script += 'then\n'
            script += '    %s\n' % hop_cmd
            script += '    exit\n'
            script += 'fi\n\n'

        script += "# ------------------------------------------------------\n"
        script += "%s"        %  cwd
        script += "%s"        %  env
        script += "%s"        %  pre
        script += "# CU execution\n"
        script += "%s %s\n\n" % (cmd, io)
        script += "%s"        %  post
        script += "# ------------------------------------------------------\n\n"

      # self._log.debug ("execution script:\n%s\n" % script)

        return script


    # --------------------------------------------------------------------------
    #
    def spawn(self, launcher, cu):

        uid = cu['_id']

        prof('ExecWorker spawn', uid=uid)

        # we got an allocation: go off and launch the process.  we get
        # a multiline command, so use the wrapper's BULK/LRUN mode.
        cmd       = self._cu_to_cmd (cu, launcher)
        run_cmd   = "BULK\nLRUN\n%s\nLRUN_EOT\nBULK_RUN\n" % cmd


      # if  self.lrms.target_is_macos :
      #     run_cmd = run_cmd.replace ("\\", "\\\\\\\\") # hello MacOS

        ret, out, _ = self.launcher_shell.run_sync (run_cmd)

        if  ret != 0 :
            self._log.error ("failed to run unit '%s': (%s)(%s)" \
                            % (run_cmd, ret, out))
            return FAIL

        lines = filter (None, out.split ("\n"))

        self._log.debug (lines)

        if  len (lines) < 2 :
            raise RuntimeError ("Failed to run unit (%s)", lines)

        if  lines[-2] != "OK" :
            raise RuntimeError ("Failed to run unit (%s)" % lines)

        # FIXME: verify format of returned pid (\d+)!
        pid           = lines[-1].strip ()
        cu['pid']     = pid
        cu['started'] = timestamp()

        # before we return, we need to clean the
        # 'BULK COMPLETED message from lrun
        ret, out = self.launcher_shell.find_prompt ()
        if  ret != 0 :
            with self._registry_lock :
                del(self._registry[uid])
            raise RuntimeError ("failed to run unit '%s': (%s)(%s)" \
                             % (run_cmd, ret, out))

        prof('spawning passed to pty', uid=uid, tag='unit spawning')

        # FIXME: this is too late, there is already a race with the monitoring
        # thread for this CU execution.  We need to communicate the PIDs/CUs via
        # a queue again!
        with self._registry_lock :
            self._registry[pid] = cu

        self._agent.update_unit_state(uid    = cu['_id'],
                                      state  = rp.EXECUTING,
                                      msg    = "unit execution started")
        # FIXME: add profiling


    # --------------------------------------------------------------------------
    #
    def _watch (self) :

        MONITOR_READ_TIMEOUT = 1.0   # check for stop signal now and then
        static_cnt           = 0

        try:

            self.monitor_shell.run_async ("MONITOR")

            while not self._terminate.is_set () :

                _, out = self.monitor_shell.find (['\n'], timeout=MONITOR_READ_TIMEOUT)

                line = out.strip ()
              # self._log.debug ('monitor line: %s' % line)

                if  not line :

                    # just a read timeout, i.e. an opportiunity to check for
                    # termination signals...
                    if  self._terminate.is_set() :
                        self._log.debug ("stop monitoring")
                        return

                    # ... and for health issues ...
                    if not self.monitor_shell.alive () :
                        self._log.warn ("monitoring channel died")
                        return

                    # ... and to handle cached events.
                    if not self._cached_events :
                        static_cnt += 1

                    else :
                        self._log.info ("monitoring channel checks cache (%d)", len(self._cached_events))
                        static_cnt += 1

                        if static_cnt == 10 :
                            # 10 times cache to check, dump it for debugging
                            print "cache state"
                            import pprint
                            pprint.pprint (self._cached_events)
                            pprint.pprint (self._registry)
                            static_cnt = 0


                        cache_copy          = self._cached_events[:]
                        self._cached_events = list()
                        events_to_handle    = list()

                        with self._registry_lock :

                            for pid, state, data in cache_copy :
                                cu = self._registry.get (pid, None)

                                if cu : events_to_handle.append ([cu, pid, state, data])
                                else  : self._cached_events.append ([pid, state, data])

                        # FIXME: measure if using many locks in the loop below
                        # is really better than doing all ops in the locked loop
                        # above
                        for cu, pid, state, data in events_to_handle :
                            self._handle_event (cu, pid, state, data)

                    # all is well...
                  # self._log.info ("monitoring channel finish idle loop")
                    continue


                elif line == 'EXIT' or line == "Killed" :
                    self._log.error ("monitoring channel failed (%s)", line)
                    self._terminate.set()
                    return

                elif not ':' in line :
                    self._log.warn ("monitoring channel noise: %s", line)

                else :
                    pid, state, data = line.split (':', 2)

                    # we are not interested in non-final state information, at
                    # the moment
                    if state in ['RUNNING'] :
                        continue

                    self._log.info ("monitoring channel event: %s", line)
                    cu = None

                    with self._registry_lock :
                        cu = self._registry.get (pid, None)
                        
                    if cu : self._handle_event (cu, pid, state, data)
                    else  : self._cached_events.append ([pid, state, data])


        except Exception as e:

            self._log.error ("Exception in job monitoring thread: %s", e)
            self._terminate.set()


    # --------------------------------------------------------------------------
    #
    def _handle_event (self, cu, pid, state, data) :

        # got an explicit event to handle
        self._log.info ("monitoring handles event for %s: %s:%s:%s", cu['_id'], pid, state, data)

        rp_state = {'DONE'     : rp.DONE, 
                    'FAILED'   : rp.FAILED, 
                    'CANCELED' : rp.CANCELED}.get (state, rp.UNKNOWN)

        if rp_state not in [rp.DONE, rp.FAILED, rp.CANCELED] :
            # non-final state
            self._log.debug ("ignore shell level state transition (%s:%s:%s)", 
                             pid, state, data)
            return

        # record timestamp, exit code on final states
        cu['finished'] = timestamp()

        if data : cu['exit_code'] = int(data)
        else    : cu['exit_code'] = None

        if rp_state in [rp.FAILED, rp.CANCELED] :
            # final state - no further state transition needed
            self._scheduler.unschedule(cu)
            self._agent.update_unit_state(uid   = cu['_id'],
                                          state = rp_state, 
                                          msg   = "unit execution finished")

        elif rp_state in [rp.DONE] :
            # advance the unit state
            self._scheduler.unschedule(cu)
            self._agent.update_unit_state(uid   = cu['_id'],
                                          state = rp.STAGING_OUTPUT,
                                          msg   = "unit execution completed")

            cu_list = blowup (cu, STAGEOUT) 
            for _cu in cu_list :
                prof('push', msg="toward stageout", uid=_cu['_id'], tag='watching')
                self._stageout_queue.put(_cu)

        # we don't need the cu in the registry anymore
        with self._registry_lock :
            if pid in self._registry :  # why wouldn't it be in there though?
                del(self._registry[pid])


# ==============================================================================
#
class UpdateWorker(threading.Thread):
    """
    An UpdateWorker pushes CU and Pilot state updates to mongodb.  Its instances
    compete for update requests on the update_queue.  Those requests will be
    triplets of collection name, query dict, and update dict.  Update requests
    will be collected into bulks over some time (BULK_COLLECTION_TIME), to
    reduce number of roundtrips.
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, agent, session_id,
                 update_queue, mongodb_url, mongodb_name, mongodb_auth):

        threading.Thread.__init__(self)

        self.name           = name
        self._log           = logger
        self._agent         = agent
        self._session_id    = session_id
        self._update_queue  = update_queue
        self._terminate     = threading.Event()

        self._mongo_db      = get_mongodb(mongodb_url, mongodb_name, mongodb_auth)
        self._cinfo         = dict()  # collection cache

        # run worker thread
        self.start()

    # --------------------------------------------------------------------------
    #
    def stop(self):
        self._terminate.set()


    # --------------------------------------------------------------------------
    #
    def run(self):

        self._log.info("started %s.", self)

        while not self._terminate.is_set():

            # ------------------------------------------------------------------
            def timed_bulk_execute(cinfo):

                # returns number of bulks pushed (0 or 1)
                if not cinfo['bulk']:
                    return 0

                now = time.time()
                age = now - cinfo['last']

                if cinfo['bulk'] and age > BULK_COLLECTION_TIME:

                    res  = cinfo['bulk'].execute()
                    self._log.debug("bulk update result: %s", res)

                    prof('state update bulk pushed (%d)' % len(cinfo['uids'].keys ()))
                    for uid in cinfo['uids']:
                        prof('state update pushed (%s)' % cinfo['uids'][uid], uid=uid)

                    cinfo['last'] = now
                    cinfo['bulk'] = None
                    cinfo['uids'] = dict()
                    return 1

                else:
                    return 0
            # ------------------------------------------------------------------

            try:

                try:
                    update_request = self._update_queue.get_nowait()

                except Queue.Empty:

                    # no new requests: push any pending bulks
                    action = 0
                    for cname in self._cinfo:
                        action += timed_bulk_execute(self._cinfo[cname])

                    if not action:
                        time.sleep(QUEUE_POLL_SLEEPTIME)

                    continue


                # got a new request.  Add to bulk (create as needed),
                # and push bulk if time is up.
                uid         = update_request.get('uid')
                state       = update_request.get('state', None)
                cbase       = update_request.get('cbase', '.cu')
                query_dict  = update_request.get('query', dict())
                update_dict = update_request.get('update',dict())

                prof('state update pulled (%s)' % state, uid=uid)

                cname = self._session_id + cbase

                if not cname in self._cinfo:
                    coll =  self._mongo_db[cname]
                    self._cinfo[cname] = {
                            'coll' : coll,
                            'bulk' : None,
                            'last' : time.time(),  # time of last push
                            'uids' : dict()
                            }

                cinfo = self._cinfo[cname]

                if not cinfo['bulk']:
                    cinfo['bulk']  = coll.initialize_ordered_bulk_op()

                cinfo['uids'][uid] = state
                cinfo['bulk'].find  (query_dict) \
                             .update(update_dict)

                timed_bulk_execute(cinfo)
              # prof('state update bulked', uid=uid)

            except Exception as e:
                self._log.exception("state update failed (%s)", e)
                # FIXME: should we fail the pilot at this point?
                # FIXME: Are the strategies to recover?


# ==============================================================================
#
class StageinWorker(threading.Thread):
    """An StageinWorker performs the agent side staging directives.
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, agent, execution_queue, schedule_queue,
                 stagein_queue, update_queue, workdir):

        threading.Thread.__init__(self)

        self.name             = name
        self._log             = logger
        self._agent           = agent
        self._execution_queue = execution_queue
        self._schedule_queue  = schedule_queue
        self._stagein_queue   = stagein_queue
        self._update_queue    = update_queue
        self._workdir         = workdir
        self._terminate       = threading.Event()

        # run worker thread
        self.start()

    # --------------------------------------------------------------------------
    #
    def stop(self):
        self._terminate.set()


    # --------------------------------------------------------------------------
    #
    def run(self):

        self._log.info("started %s.", self)

        while not self._terminate.is_set():

            try:

                cu = self._stagein_queue.get()

                if not cu:
                    continue

                sandbox      = os.path.join(self._workdir, '%s' % cu['_id'])
                staging_area = os.path.join(self._workdir, STAGING_AREA)

                for directive in cu['Agent_Input_Directives']:
                    prof('Agent input_staging queue', uid=cu['_id'], msg=directive)

                    # Perform input staging
                    self._log.info("unit input staging directives %s for cu: %s to %s",
                                   directive, cu['_id'], sandbox)

                    # Convert the source_url into a SAGA Url object
                    source_url = saga.Url(directive['source'])

                    # Handle special 'staging' scheme
                    if source_url.scheme == 'staging':
                        self._log.info('Operating from staging')
                        # Remove the leading slash to get a relative path from the staging area
                        rel2staging = source_url.path.split('/',1)[1]
                        source = os.path.join(staging_area, rel2staging)
                    else:
                        self._log.info('Operating from absolute path')
                        source = source_url.path

                    # Get the target from the directive and convert it to the location
                    # in the sandbox
                    target = directive['target']
                    abs_target = os.path.join(sandbox, target)

                    # Create output directory in case it doesn't exist yet
                    #
                    rec_makedir(os.path.dirname(abs_target))

                    try:
                        self._log.info("Going to '%s' %s to %s", directive['action'], source, abs_target)

                        if   directive['action'] == LINK: os.symlink     (source, abs_target)
                        elif directive['action'] == COPY: shutil.copyfile(source, abs_target)
                        elif directive['action'] == MOVE: shutil.move    (source, abs_target)
                        else:
                            # FIXME: implement TRANSFER mode
                            raise NotImplementedError('Action %s not supported' % directive['action'])

                        log_message = "%s'ed %s to %s - success" % (directive['action'], source, abs_target)
                        self._log.info(log_message)

                        # If all went fine, update the state of this
                        # StagingDirective to DONE
                        # FIXME: is this update below really *needed*?
                        self._agent.update_unit(uid    = cu['_id'],
                                                msg    = log_message,
                                                query  = {
                                                    'Agent_Input_Status'            : rp.EXECUTING,
                                                    'Agent_Input_Directives.state'  : rp.PENDING,
                                                    'Agent_Input_Directives.source' : directive['source'],
                                                    'Agent_Input_Directives.target' : directive['target']
                                                },
                                                update = {
                                                    '$set' : {'Agent_Input_Directives.$.state' : rp.DONE}
                                                })
                    except Exception as e:

                        # If we catch an exception, assume the staging failed
                        log_message = "%s'ed %s to %s - failure (%s)" % \
                                (directive['action'], source, abs_target, e)
                        self._log.exception(log_message)

                        # If a staging directive fails, fail the CU also.
                        self._agent.update_unit_state(uid    = cu['_id'],
                                                      state  = rp.FAILED,
                                                      msg    = log_message,
                                                      query  = {
                                                          'Agent_Input_Status'             : rp.EXECUTING,
                                                          'Agent_Input_Directives.state'   : rp.PENDING,
                                                          'Agent_Input_Directives.source'  : directive['source'],
                                                          'Agent_Input_Directives.target'  : directive['target']
                                                      },
                                                      update = {
                                                          '$set' : {'Agent_Input_Directives.$.state'  : rp.FAILED,
                                                                    'Agent_Input_Status'              : rp.FAILED}
                                                      })

                # cu staging is all done, unit can go to execution
                self._agent.update_unit_state(uid    = cu['_id'],
                                              state  = rp.ALLOCATING,
                                              msg    = 'agent input staging done')
                cu_list = blowup (cu, SCHEDULE) 
                for _cu in cu_list :
                    prof('push', msg="towards scheduling", uid=_cu['_id'], tag='stagein')
                    self._schedule_queue.put(_cu)


            except Exception as e:
                self._log.exception('worker died')
                sys.exit(1)


# ==============================================================================
#
class StageoutWorker(threading.Thread):
    """
    An StageoutWorker performs the agent side staging directives.

    It competes for units on the stageout queue, and handles all relevant
    staging directives.  It also takes care of uploading stdout/stderr (which
    can also be considered staging, really).

    Upon completion, the units are moved into the respective final state.

    Multiple StageoutWorker instances can co-exist -- this class needs to be
    threadsafe.
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, agent, execution_queue, stageout_queue, update_queue, workdir):

        threading.Thread.__init__(self)

        self.name             = name
        self._log             = logger
        self._agent           = agent
        self._execution_queue = execution_queue
        self._stageout_queue  = stageout_queue
        self._update_queue    = update_queue
        self._workdir         = workdir
        self._terminate       = threading.Event()

        # run worker thread
        self.start()

    # --------------------------------------------------------------------------
    #
    def stop(self):
        self._terminate.set()


    # --------------------------------------------------------------------------
    #
    def run(self):

        self._log.info("started %s.", self)

        staging_area = os.path.join(self._workdir, STAGING_AREA)

        while not self._terminate.is_set():

            cu = None
            try:

                cu = self._stageout_queue.get()

                if not cu:
                    continue

                sandbox = os.path.join(self._workdir, '%s' % cu['_id'])

                ## parked from unit state checker: unit postprocessing

                if os.path.isfile(cu['stdout_file']):
                    with open(cu['stdout_file'], 'r') as stdout_f:
                        try:
                            txt = unicode(stdout_f.read(), "utf-8")
                        except UnicodeDecodeError:
                            txt = "unit stdout contains binary data -- use file staging directives"

                        cu['stdout'] += tail(txt)


                if os.path.isfile(cu['stderr_file']):
                    with open(cu['stderr_file'], 'r') as stderr_f:
                        try:
                            txt = unicode(stderr_f.read(), "utf-8")
                        except UnicodeDecodeError:
                            txt = "unit stderr contains binary data -- use file staging directives"

                        cu['stderr'] += tail(txt)


                for directive in cu['Agent_Output_Directives']:

                    # Perform output staging

                    self._log.info("unit output staging directives %s for cu: %s to %s",
                            directive, cu['_id'], sandbox)

                    # Convert the target_url into a SAGA Url object
                    target_url = saga.Url(directive['target'])

                    # Handle special 'staging' scheme
                    if target_url.scheme == 'staging':
                        self._log.info('Operating from staging')
                        # Remove the leading slash to get a relative path from
                        # the staging area
                        rel2staging = target_url.path.split('/',1)[1]
                        target = os.path.join(staging_area, rel2staging)
                    else:
                        self._log.info('Operating from absolute path')
                        # FIXME: will this work for TRANSFER mode?
                        target = target_url.path

                    # Get the source from the directive and convert it to the location
                    # in the sandbox
                    source = str(directive['source'])
                    abs_source = os.path.join(sandbox, source)

                    # Create output directory in case it doesn't exist yet
                    # FIXME: will this work for TRANSFER mode?
                    rec_makedir(os.path.dirname(target))

                    try:
                        self._log.info("Going to '%s' %s to %s", directive['action'], abs_source, target)

                        if   directive['action'] == LINK: os.symlink     (abs_source, target)
                        elif directive['action'] == COPY: shutil.copyfile(abs_source, target)
                        elif directive['action'] == MOVE: shutil.move    (abs_source, target)
                        else:
                            # FIXME: implement TRANSFER mode
                            raise NotImplementedError('Action %s not supported' % directive['action'])

                        log_message = "%s'ed %s to %s - success" %(directive['action'], abs_source, target)
                        self._log.info(log_message)

                        # If all went fine, update the state of this
                        # StagingDirective to DONE
                        # FIXME: is this update below really *needed*?
                        self._agent.update_unit(uid    = cu['_id'],
                                                msg    = log_message,
                                                query  = {
                                                    'Agent_Output_Status'           : rp.EXECUTING,
                                                    'Agent_Output_Directives.state' : rp.PENDING,
                                                    'Agent_Output_Directives.source': directive['source'],
                                                    'Agent_Output_Directives.target': directive['target']
                                                },
                                                update = {
                                                    '$set' : {'Agent_Output_Directives.$.state': rp.DONE}
                                                })
                    except Exception as e:
                        # If we catch an exception, assume the staging failed
                        log_message = "%s'ed %s to %s - failure (%s)" % \
                                (directive['action'], abs_source, target, e)
                        self._log.exception(log_message)

                        # If a staging directive fails, fail the CU also.
                        self._agent.update_unit_state(uid    = cu['_id'],
                                                      state  = rp.FAILED,
                                                      msg    = log_message,
                                                      query  = {
                                                          'Agent_Output_Status'            : rp.EXECUTING,
                                                          'Agent_Output_Directives.state'  : rp.PENDING,
                                                          'Agent_Output_Directives.source' : directive['source'],
                                                          'Agent_Output_Directives.target' : directive['target']
                                                      },
                                                      update = {
                                                          '$set' : {'Agent_Output_Directives.$.state' : rp.FAILED,
                                                                    'Agent_Output_Status'             : rp.FAILED}
                                                      })


                # local staging is done. Now check if there are Directives that
                # need to be performed by the FTW.
                # Obviously these are not executed here (by the Agent),
                # but we need this code to set the state so that the FTW
                # gets notified that it can start its work.
                if cu['FTW_Output_Directives']:

                    prof('ExecWorker unit needs FTW_O ', uid=cu['_id'])
                    self._agent.update_unit(
                        uid    = cu['_id'],
                        msg    = 'FTW output staging needed',
                        update = {
                            '$set': {
                                'FTW_Output_Status' : rp.PENDING,
                                'stdout'            : cu['stdout'],
                                'stderr'            : cu['stderr'],
                                'exit_code'         : cu['exit_code'],
                                'started'           : cu['started'],
                                'finished'          : cu['finished'],
                                'slots'             : cu['opaque_slot'],
                            }
                        })
                    # NOTE: this is final for the agent scope -- further state
                    # transitions are done by the FTW.
                    cu = None

                else:
                    # no FTW staging is needed, local staging is done -- we can
                    # move the unit into final state.
                    prof('final', msg="stageout done", uid=cu['_id'], tag='stageout')
                    self._agent.update_unit_state(uid    = cu['_id'],
                                                  state  = rp.DONE,
                                                  msg    = 'output staging completed',
                                                  update = {
                                                      '$set' : {
                                                          'stdout'    : cu['stdout'],
                                                          'stderr'    : cu['stderr'],
                                                          'exit_code' : cu['exit_code'],
                                                          'started'   : cu['started'],
                                                          'finished'  : cu['finished'],
                                                          'slots'     : cu['opaque_slot'],
                                                      }
                                                  })
                    # NOTE: this is final, the cu is not touched anymore
                    cu = None

                # make sure the CU is not touched anymore (see except below)
                cu = None

            except Exception as e:
                self._log.exception("Error in StageoutWorker loop (%s)", e)

                # check if we have any cu in operation.  If so, mark as final.
                # This check relies on the pushes to the update queue to be the
                # *last* actions of the loop above -- otherwise we may get
                # invalid state transitions...
                if cu:
                    prof('final', msg="stageout failed", uid=cu['_id'], tag='stageout')
                    self._agent.update_unit_state(uid    = cu['_id'],
                                                  state  = rp.FAILED,
                                                  msg    = 'output staging failed',
                                                  update = {
                                                      '$set' : {
                                                          'stdout'    : cu['stdout'],
                                                          'stderr'    : cu['stderr'],
                                                          'exit_code' : cu['exit_code'],
                                                          'started'   : cu['started'],
                                                          'finished'  : cu['finished'],
                                                          'slots'     : cu['opaque_slot'],
                                                      }
                                                  })
                    # NOTE: this is final, the cu is not touched anymore
                    cu = None
                raise


# ==============================================================================
#
class HeartbeatMonitor(threading.Thread):
    """
    The HeartbeatMonitor watches the command queue for heartbeat updates (and
    other commands).
    """

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, agent, command_queue, p, pilot_id, starttime, runtime):

        threading.Thread.__init__(self)

        self.name             = name
        self._log             = logger
        self._agent           = agent
        self._command_queue   = command_queue
        self._p               = p
        self._pilot_id        = pilot_id
        self._starttime       = starttime
        self._runtime         = runtime
        self._terminate       = threading.Event()

        # run worker thread
        self.start()

    # --------------------------------------------------------------------------
    #
    def stop(self):

        self._terminate.set()
        self._agent.stop()


    # --------------------------------------------------------------------------
    #
    def run(self):

        self._log.info("started %s.", self)

        while not self._terminate.is_set():

            try:
                prof('heartbeat', msg='Listen! Listen! Listen to the heartbeat!')
                self._check_commands()
                self._check_state   ()
                time.sleep(HEARTBEAT_INTERVAL)

            except Exception as e:
                self._log.exception('error in heartbeat monitor (%s)', e)
                self.stop()


    # --------------------------------------------------------------------------
    #
    def _check_commands(self):

        # Check if there's a command waiting
        retdoc = self._p.find_and_modify(
                    query  = {"_id"  : self._pilot_id},
                    update = {"$set" : {COMMAND_FIELD: []}}, # Wipe content of array
                    fields = [COMMAND_FIELD, 'state']
                    )

        commands = list()
        if retdoc:
            commands = retdoc[COMMAND_FIELD]
            state    = retdoc['state']


        for command in commands:

            prof('Monitor get command', msg=[command[COMMAND_TYPE], command[COMMAND_ARG]])

            if command[COMMAND_TYPE] == COMMAND_CANCEL_PILOT:
                self.stop()
                pilot_CANCELED(self._p, self._pilot_id, self._log, "CANCEL received. Terminating.")
                sys.exit(1)

            elif state == rp.CANCELING:
                self.stop()
                pilot_CANCELED(self._p, self._pilot_id, self._log, "CANCEL implied. Terminating.")
                sys.exit(1)

            elif command[COMMAND_TYPE] == COMMAND_CANCEL_COMPUTE_UNIT:
                self._log.info("Received Cancel Compute Unit command for: %s", command[COMMAND_ARG])
                # Put it on the command queue of the ExecWorker
                self._command_queue.put(command)

            elif command[COMMAND_TYPE] == COMMAND_KEEP_ALIVE:
                self._log.info("Received KeepAlive command.")

            else:
                self._log.error("Received unknown command: %s with arg: %s.",
                                command[COMMAND_TYPE], command[COMMAND_ARG])


    # --------------------------------------------------------------------------
    #
    def _check_state(self):

        # Check the workers periodically. If they have died, we
        # exit as well. this can happen, e.g., if the worker
        # process has caught an exception
        for worker in self._agent.worker_list:
            if not worker.is_alive():
                self.stop()
                msg = 'worker %s died' % str(worker)
                pilot_FAILED(self._p, self._pilot_id, self._log, msg)

        # Make sure that we haven't exceeded the agent runtime. if
        # we have, terminate.
        if time.time() >= self._starttime + (int(self._runtime) * 60):
            self._log.info("Agent has reached runtime limit of %s seconds.", self._runtime*60)
            self.stop()
            pilot_DONE(self._p, self._pilot_id)



# ==============================================================================
#
class Agent(object):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, logger, lrms_name, requested_cores,
            task_launch_method, mpi_launch_method, spawner,
            scheduler_name, runtime,
            mongodb_url, mongodb_name, mongodb_auth,
            pilot_id, session_id):

        prof('Agent init')

        self.name                   = name
        self._log                   = logger
        self._debug_helper          = ru.DebugHelper()
        self._pilot_id              = pilot_id
        self._runtime               = runtime
        self._terminate             = threading.Event()
        self._starttime             = time.time()
        self._workdir               = os.getcwd()
        self._session_id            = session_id
        self._pilot_id              = pilot_id

        self.worker_list            = list()

        # we want to own all queues -- that simplifies startup and shutdown
        self._schedule_queue        = QUEUE_TYPE()
        self._stagein_queue         = QUEUE_TYPE()
        self._execution_queue       = QUEUE_TYPE()
        self._stageout_queue        = QUEUE_TYPE()
        self._update_queue          = QUEUE_TYPE()
        self._command_queue         = QUEUE_TYPE()

        mongo_db = get_mongodb(mongodb_url, mongodb_name, mongodb_auth)

        self._p  = mongo_db["%s.p"  % self._session_id]
        self._cu = mongo_db["%s.cu" % self._session_id]

        self._lrms = LRMS.create(
                name            = lrms_name,
                logger          = self._log,
                requested_cores = requested_cores)

        self._scheduler = Scheduler.create(
                name            = scheduler_name,
                logger          = self._log,
                lrms            = self._lrms,
                schedule_queue  = self._schedule_queue,
                execution_queue = self._execution_queue,
                update_queue    = self._update_queue)
        self.worker_list.append(self._scheduler)

        self._task_launcher = LaunchMethod.create(
                name            = task_launch_method,
                logger          = self._log,
                scheduler       = self._scheduler)

        self._mpi_launcher = LaunchMethod.create(
                name            = mpi_launch_method,
                logger          = self._log,
                scheduler       = self._scheduler)

        for n in range(NUMBER_OF_WORKERS[STAGEIN]):
            stagein_worker = StageinWorker(
                name            = "StageinWorker-%d" % n,
                logger          = self._log,
                agent           = self,
                execution_queue = self._execution_queue,
                schedule_queue  = self._schedule_queue,
                stagein_queue   = self._stagein_queue,
                update_queue    = self._update_queue,
                workdir         = self._workdir
            )
            self.worker_list.append(stagein_worker)


        for n in range(NUMBER_OF_WORKERS[EXEC]):
            exec_worker = ExecWorker.create(
                name            = "ExecWorker-%d" % n,
                spawner         = spawner,
                logger          = self._log,
                agent           = self,
                lrms            = self._lrms,
                scheduler       = self._scheduler,
                task_launcher   = self._task_launcher,
                mpi_launcher    = self._mpi_launcher,
                command_queue   = self._command_queue,
                execution_queue = self._execution_queue,
                stageout_queue  = self._stageout_queue,
                update_queue    = self._update_queue,
                pilot_id        = self._pilot_id,
                session_id      = self._session_id
            )
            self.worker_list.append(exec_worker)


        for n in range(NUMBER_OF_WORKERS[STAGEOUT]):
            stageout_worker = StageoutWorker(
                name            = "StageoutWorker-%d" % n,
                agent           = self,
                logger          = self._log,
                execution_queue = self._execution_queue,
                stageout_queue  = self._stageout_queue,
                update_queue    = self._update_queue,
                workdir         = self._workdir
            )
            self.worker_list.append(stageout_worker)


        for n in range(NUMBER_OF_WORKERS[UPDATE]):
            update_worker = UpdateWorker(
                name            = "UpdateWorker-%d" % n,
                agent           = self,
                logger          = self._log,
                session_id      = self._session_id,
                update_queue    = self._update_queue,
                mongodb_url     = mongodb_url,
                mongodb_name    = mongodb_name,
                mongodb_auth    = mongodb_auth
            )
            self.worker_list.append(update_worker)


        hbmon = HeartbeatMonitor(
                name            = "HeartbeatMonitor",
                logger          = self._log,
                agent           = self,
                command_queue   = self._command_queue,
                p               = self._p,
                starttime       = self._starttime,
                runtime         = self._runtime,
                pilot_id        = self._pilot_id)
        self.worker_list.append(hbmon)

        prof('Agent init done')


    # --------------------------------------------------------------------------
    #
    def stop(self):
        """
        Terminate the agent main loop.  The workers will be pulled down once the
        main loop finishes (see run())
        """

        prof('Agent stop()')
        self._terminate.set()


    # --------------------------------------------------------------------------
    #
    def update_unit(self, uid, state=None, msg=None, query=None, update=None):

        if not query  : query  = dict()
        if not update : update = dict()

        query_dict  = dict()
        update_dict = update

        query_dict['_id'] = uid

        for key,val in query.iteritems():
            query_dict[key] = val


        if msg:
            if not '$push' in update_dict:
                update_dict['$push'] = dict()

            update_dict['$push']['log'] = {'message'   : msg,
                                           'timestamp' : timestamp()}

        query_list = blowup (query_dict, UPDATE) 
        for query_dict in query_list :
            prof('push', msg="towards update (%s)" % state, uid=query_dict['_id'])
            self._update_queue.put({'uid'    : query_dict['_id'],
                                    'state'  : state,
                                    'cbase'  : '.cu',
                                    'query'  : query_dict,
                                    'update' : update_dict})


    # --------------------------------------------------------------------------
    #
    def update_unit_state(self, uid, state, msg=None, query=None, update=None,
            logger=None):

        if not query  : query  = dict()
        if not update : update = dict()

        if  logger and msg:
            logger("unit '%s' state change (%s)" % (uid, msg))

        # we alter update, so rather use a copy of the dict...

        now = timestamp()
        update_dict = {
                '$set' : {
                    'state' : state
                },
                '$push': {
                    'statehistory' : {
                        'state'     : state,
                        'timestamp' : now
                    }
                }
            }

        if '$set' in update:
            for key,val in update['$set'].iteritems():
                update_dict['$set'][key] = val

        if '$push' in update:
            for key,val in update['$push'].iteritems():
                update_dict['$push'][key] = val

        self.update_unit(uid    = uid,
                         state  = state,
                         msg    = msg,
                         query  = query,
                         update = update_dict)


    # --------------------------------------------------------------------------
    #
    def run(self):

        self._log.info("started %s.", self)
        prof('Agent run()')

        # first order of business: set the start time and state of the pilot
        self._log.info("Agent %s starting ...", self._pilot_id)
        now = timestamp()
        ret = self._p.update(
            {"_id": self._pilot_id},
            {"$set": {"state"          : rp.ACTIVE,
                      # TODO: The two fields below are currently scheduler
                      #       specific!
                      "nodes"          : self._lrms.node_list,
                      "cores_per_node" : self._lrms.cores_per_node,
                      "started"        : now},
             "$push": {"statehistory": {"state"    : rp.ACTIVE,
                                        "timestamp": now}}
            })
        # TODO: Check for return value, update should be true!
        self._log.info("Database updated: %s", ret)

        prof('Agent start loop')

        while not self._terminate.is_set():

            try:

                # check for new units
                action = self._check_units()

                # if no units have been seen, then wait for juuuust a little...
                # FIXME: use some mongodb notification mechanism to avoid busy
                # polling.  Tailed cursors or whatever...
                if not action:
                    time.sleep(DB_POLL_SLEEPTIME)

            except Exception as e:
                # exception in the main loop is fatal
                self.stop()
                pilot_FAILED(self._p, self._pilot_id, self._log,
                    "ERROR in agent main loop: %s. %s" % (e, traceback.format_exc()))
                sys.exit(1)

        # main loop terminated, so self._terminate was set
        # we need to signal shut down to all workers
        for worker in self.worker_list:
            worker.stop()

        # to make sure that threads are not stuck waiting on a queue, we send
        # a signal on each queue
        self._schedule_queue.put (None)
        self._execution_queue.put(None)
        self._update_queue.put   (None)
        self._stagein_queue.put  (None)
        self._stageout_queue.put (None)

        # and wait for them to actually finish
        # FIXME: make sure this works when stop was initialized by heartbeat monitor
        for worker in self.worker_list:
            worker.join()

        # record cancelation state
        pilot_CANCELED(self._p, self._pilot_id, self._log,
                "Terminated (_terminate set).")

        sys.exit(0)


    # --------------------------------------------------------------------------
    #
    def _check_units(self):

        # Check if there are compute units waiting for execution,
        # and log that we pulled it.
        #
        # Unfortunately, 'find_and_modify' is not bulkable, so we have to use
        # 'find' above.  To avoid finding the same units over and over again, we
        # have to update the state *before* running the next find -- so we
        # do it right here...  No idea how to avoid that roundtrip...
        # This also blocks us from using multiple ingest threads, or from doing
        # late binding by unit pull...
        cu_cursor  = self._cu.find(multi = True,
                                   spec  = {"pilot" : self._pilot_id,
                                            "state" : rp.PENDING_EXECUTION})

        if cu_cursor.count():

            cu_list = list(cu_cursor)
            cu_list = blowup (cu_list, INGEST)
            cu_uids = [_cu['_id'] for _cu in cu_list]

            self._cu.update(
                    multi    = True,
                    spec     = {"_id"   : {"$in"    : cu_uids}},
                    document = {"$set"  : {"state"  : rp.ALLOCATING},
                                "$push" : {"statehistory":
                                    {
                                        "state"     : rp.ALLOCATING,
                                        "timestamp" : timestamp()
                                    }
                               }})
        else :
            # if we did not find any units which can be executed immediately, we
            # chack if we have any units for which to do stage-in
            cu_cursor = self._cu.find(multi = True,
                                      spec  = {"pilot" : self._pilot_id,
                                               'Agent_Input_Status': rp.PENDING})
            if cu_cursor.count():

                cu_list = list(cu_cursor)
                cu_list = blowup (cu_list, INGEST)
                cu_uids = [_cu['_id'] for _cu in cu_list]

                self._cu.update(
                        multi    = True,
                        spec     = {"_id"   : {"$in"    : cu_uids}},
                        document = {"$set"  : {"state"  : rp.STAGING_INPUT,
                                               "Agent_Input_Status": rp.EXECUTING},
                                    "$push" : {"statehistory":
                                        {
                                            "state"     : rp.STAGING_INPUT,
                                            "timestamp" : timestamp()
                                        }
                                   }})
            else :
                # no units whatsoever...
                return 0

        # now we really own the CUs, and can start working on them (ie. push
        # them into the pipeline)
        if cu_cursor.count():
            prof('Agent get units', msg="number of units: %d" % cu_cursor.count(),
                 logger=self._log.info)


        for cu in cu_list:

            try:
                prof('Agent get unit', uid=cu['_id'], tag='cu arriving',
                     logger=self._log.info)

                prof('Agent get unit ingest', uid=cu['_id'])

                cud     = cu['description']
                workdir = "%s/%s" % (self._workdir, cu['_id'])

                cu['workdir']     = workdir
                cu['stdout']      = ''
                cu['stderr']      = ''
                cu['opaque_clot'] = None

                stdout_file = cud.get('stdout')
                if not stdout_file:
                    stdout_file = 'STDOUT'
                cu['stdout_file'] = os.path.join(workdir, stdout_file)

                stderr_file = cud.get('stderr')
                if not stderr_file:
                    stderr_file = 'STDERR'
                cu['stderr_file'] = os.path.join(workdir, stderr_file)


                prof('Agent get unit meta', uid=cu['_id'])
                # create unit sandbox
                rec_makedir(workdir)
                prof('Agent get unit mkdir', uid=cu['_id'])

                # and send to staging / execution, respectively
                if cu['Agent_Input_Directives']:

                    self.update_unit_state(uid    = cu['_id'],
                                           state  = rp.STAGING_INPUT,
                                           msg    = 'unit needs input staging')
                    cu_list = blowup (cu, STAGEIN) 
                    for _cu in cu_list :
                        prof('push', msg="towards stagein", uid=_cu['_id'], tag='ingest')
                        self._stagein_queue.put(_cu)

                else:
                    self.update_unit_state(uid    = cu['_id'],
                                           state  = rp.ALLOCATING,
                                           msg    = 'unit needs no input staging')
                    cu_list = blowup (cu, SCHEDULE) 
                    for _cu in cu_list :
                        prof('push', msg="towards scheduling", uid=_cu['_id'], tag='ingest')
                        self._schedule_queue.put(_cu)


            except Exception as e:
                # if any unit sorting step failed, the unit did not end up in
                # a queue (its always the last step).  We set it to FAILED
                msg = "could not sort unit (%s)" % e
                prof('error', msg=msg, tag="failed", uid=cu['_id'], logger=self._log.exception)
                self.update_unit_state(uid    = cu['_id'],
                                       state  = rp.FAILED,
                                       msg    = msg)
                # NOTE: this is final, the unit will not be touched
                # anymore.
                cu = None


        # indicate that we did some work (if we did...)
        return len(cu_uids)


# ==============================================================================
#
# Agent main code
#
# ==============================================================================
def main():

    mongo_p = None
    parser  = optparse.OptionParser()

    parser.add_option('-a', dest='mongodb_auth')
    parser.add_option('-c', dest='cores',       type='int')
    parser.add_option('-d', dest='debug_level', type='int')
    parser.add_option('-j', dest='task_launch_method')
    parser.add_option('-k', dest='mpi_launch_method')
    parser.add_option('-l', dest='lrms')
    parser.add_option('-m', dest='mongodb_url')
    parser.add_option('-n', dest='mongodb_name')
    parser.add_option('-o', dest='spawner')
    parser.add_option('-p', dest='pilot_id')
    parser.add_option('-q', dest='agent_scheduler')
    parser.add_option('-r', dest='runtime',     type='int')
    parser.add_option('-s', dest='session_id')

    # parse the whole shebang
    (options, args) = parser.parse_args()

    if args : parser.error("Unused arguments '%s'" % args)

    if not options.cores                : parser.error("Missing number of cores (-c)")
    if not options.debug_level          : parser.error("Missing DEBUG level (-d)")
    if not options.task_launch_method   : parser.error("Missing unit launch method (-j)")
    if not options.mpi_launch_method    : parser.error("Missing mpi launch method (-k)")
    if not options.lrms                 : parser.error("Missing LRMS (-l)")
    if not options.mongodb_url          : parser.error("Missing MongoDB URL (-m)")
    if not options.mongodb_name         : parser.error("Missing database name (-n)")
    if not options.spawner              : parser.error("Missing agent spawner (-o)")
    if not options.pilot_id             : parser.error("Missing pilot id (-p)")
    if not options.agent_scheduler      : parser.error("Missing agent scheduler (-q)")
    if not options.runtime              : parser.error("Missing agent runtime (-r)")
    if not options.session_id           : parser.error("Missing session id (-s)")

    prof('start', tag='bootstrapping', uid=options.pilot_id)

    # configure the agent logger
    logger    = logging.getLogger  ('radical.pilot.agent')
    handle    = logging.FileHandler("agent.log")
    formatter = logging.Formatter  ('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    logger.setLevel(options.debug_level)
    handle.setFormatter(formatter)
    logger.addHandler(handle)

    logger.info("Using SAGA version %s", saga.version)
    logger.info("Using RADICAL-Pilot multicore agent, version %s", git_ident)
  # logger.info("Using RADICAL-Pilot version %s", rp.version)


    # --------------------------------------------------------------------------
    #
    def sigint_handler(signum, frame):
        msg = 'Caught SIGINT. EXITING. (%s: %s)' % (signum, frame)
        pilot_FAILED(mongo_p, options.pilot_id, logger, msg)
        sys.exit(2)
    signal.signal(signal.SIGINT, sigint_handler)


    # --------------------------------------------------------------------------
    #
    def sigalarm_handler(signum, frame):
        msg = 'Caught SIGALRM (Walltime limit reached?). EXITING (%s: %s)' \
            % (signum, frame)
        pilot_FAILED(mongo_p, options.pilot_id, logger, msg)
        sys.exit(3)
    signal.signal(signal.SIGALRM, sigalarm_handler)


    # --------------------------------------------------------------------------
    # load the local agent config, and overload the config dicts
    try : 
        logger.info ("load agent config")
        cfg_file = "%s/.radical/pilot/configs/agent.json" % os.environ['HOME']
        cfg      = ru.read_json_str (cfg_file)
    
        ru.dict_merge (NUMBER_OF_WORKERS, cfg.get ('NUMBER_OF_WORKERS', {}), policy='overwrite')
        ru.dict_merge (BLOWUP_FACTOR,     cfg.get ('BLOWUP_FACTOR',     {}), policy='overwrite')
        ru.dict_merge (DROP_CLONES,       cfg.get ('DROP_CLONES',       {}), policy='overwrite')
    
    except Exception as e:
        logger.info ("agent config not merged: %s", e)


    try:
        # ----------------------------------------------------------------------
        # Establish database connection
        prof('db setup')
        mongo_db = get_mongodb(options.mongodb_url, options.mongodb_name,
                               options.mongodb_auth)
        mongo_p  = mongo_db["%s.p" % options.session_id]


        # ----------------------------------------------------------------------
        # Launch the agent thread
        prof('Agent create')
        agent = Agent(
                name               = 'Agent',
                logger             = logger,
                lrms_name          = options.lrms,
                requested_cores    = options.cores,
                task_launch_method = options.task_launch_method,
                mpi_launch_method  = options.mpi_launch_method,
                spawner            = options.spawner,
                scheduler_name     = options.agent_scheduler,
                runtime            = options.runtime,
                mongodb_url        = options.mongodb_url,
                mongodb_name       = options.mongodb_name,
                mongodb_auth       = options.mongodb_auth,
                pilot_id           = options.pilot_id,
                session_id         = options.session_id
        )

        agent.run()
        prof('Agent done')

    except SystemExit:
        logger.error("Caught keyboard interrupt. EXITING")
        return(6)

    except Exception as e:
        error_msg = "Error running agent: %s" % str(e)
        logger.exception(error_msg)
        pilot_FAILED(mongo_p, options.pilot_id, logger, error_msg)
        sys.exit(7)

    finally:
        prof('stop', msg='finally clause')
        sys.exit(8)


# ------------------------------------------------------------------------------
#
if __name__ == "__main__":

    sys.exit(main())

#
# ------------------------------------------------------------------------------

