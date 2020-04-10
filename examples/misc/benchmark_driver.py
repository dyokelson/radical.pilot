#!/usr/bin/env python
# pylint: disable=redefined-outer-name
__copyright__ = "Copyright 2013-2014, http://radical.rutgers.edu"
__license__   = "MIT"

import os
import sys
import time
import radical.pilot as rp

# READ: The RADICAL-Pilot documentation: 
#   https://radicalpilot.readthedocs.io/en/stable/
#
# Try running this example with RADICAL_PILOT_VERBOSE=debug set if 
# you want to see what happens behind the scenes!


# ------------------------------------------------------------------------------
#
def pilot_state_cb (pilot, state):
    """ this callback is invoked on all pilot state changes """

    print("[Callback]: ComputePilot '%s' state: %s." % (pilot.uid, state))

    if state == rp.FAILED:
        sys.exit (1)


# ------------------------------------------------------------------------------
#
def unit_state_cb (unit, state):
    """ this callback is invoked on all unit state changes """

    print("[Callback]: ComputeUnit  '%s' state: %s." % (unit.uid, state))

    if state == rp.FAILED:
        sys.exit (1)


# ------------------------------------------------------------------------------
#
if __name__ == "__main__":

    # we can optionally pass session name to RP
    if len(sys.argv) > 1:
        session_name = sys.argv[1]
    else:
        session_name = None

    # Create a new session. No need to try/except this: if session creation
    # fails, there is not much we can do anyways...
    session = rp.Session(uid=session_name)
    print("session id: %s" % session.uid)

    # all other pilot code is now tried/excepted.  If an exception is caught, we
    # can rely on the session object to exist and be valid, and we can thus tear
    # the whole RP stack down via a 'session.close()' call in the 'finally'
    # clause...
    try:

        rp_cores    = int(os.getenv("RP_CORES",    '16'))
        rp_cu_cores = int(os.getenv("RP_CU_CORES", '1'))
        rp_units    = int(os.getenv("RP_UNITS",    str(rp_cores * 3 * 3 * 2)))  # 3 units/core/pilot
        rp_runtime  = int(os.getenv("RP_RUNTIME",  '15'))
        rp_user     = str(os.getenv("RP_USER",     ""))
        rp_host     = str(os.getenv("RP_HOST",     "local.localhost"))
        rp_queue    = str(os.getenv("RP_QUEUE",    ""))

        # make jenkins happy
        c         = rp.Context ('ssh')
        c.user_id = rp_user
        session.add_context (c)

        # Add a Pilot Manager. Pilot managers manage one or more ComputePilots.
        pmgr = rp.PilotManager(session=session)

        # Register our callback with the PilotManager. This callback will get
        # called every time any of the pilots managed by the PilotManager
        # change their state.
        pmgr.register_callback(pilot_state_cb)

        # Define 1-core local pilots that run for 10 minutes and clean up
        # after themself.
        pdescriptions = list()
        for i in [1, 2, 3]:
            pdesc = rp.ComputePilotDescription()
            pdesc.resource = rp_host
            pdesc.runtime  = rp_runtime
            pdesc.cores    = i * rp_cores
            pdesc.cleanup  = False
            if rp_queue  : pdesc.queue    = rp_queue

            pdescriptions.append(pdesc)


        # Launch the pilots.
        pilots = pmgr.submit_pilots (pdescriptions)
        print("pilots: %s" % pilots)

        # Combine the ComputePilot, the ComputeUnits and a scheduler via
        # a UnitManager object.
        umgr = rp.UnitManager(
            session=session,
            scheduler=rp.SCHEDULER_BACKFILLING)
        # Register our callback with the UnitManager. This callback will get
        # called every time any of the units managed by the UnitManager
        # change their state.
        umgr.register_callback(unit_state_cb)

        # Add the previsouly created ComputePilots to the UnitManager.
        umgr.add_pilots(pilots)


        # Create a workload of n ComputeUnits.
        cu_descriptions = []

        for unit_count in range(0, rp_units):
            cu = rp.ComputeUnitDescription()
            cu.executable  = "/bin/sleep"
            cu.arguments   = ["30"]

            import pprint
            pprint.pprint (cu)

            cu_descriptions.append(cu)

        # Submit the ComputeUnit descriptions to the UnitManager. This will trigger
        # the selected scheduler to start assigning the units to the pilots.
        units = umgr.submit_units(cu_descriptions)
        print("units: %s" % umgr.list_units ())

        # Wait for all compute units to reach a terminal state (DONE or FAILED).
        umgr.wait_units()

        if not isinstance (units, list):
            units = [units]

        for unit in units:
            print("* Task %s state: %s, exit code: %s"
                  % (unit.uid, unit.state, unit.exit_code))

        # Close automatically cancels the pilot(s).
        pmgr.cancel_pilots ()
        time.sleep (3)

        # run the stats plotter
        #os.system ("bin/radicalpilot-stats -m plot -s %s" % session.uid) 
        #os.system ("cp -v %s.png report/rp.benchmark.png" % session.uid) 

    except Exception as e:
        # Something unexpected happened in the pilot code above
        print("caught Exception: %s" % e)
        raise

    except (KeyboardInterrupt, SystemExit) as e:
        # the callback called sys.exit(), and we can here catch the
        # corresponding KeyboardInterrupt exception for shutdown.  We also catch
        # SystemExit (which gets raised if the main threads exits for some other
        # reason).
        print("need to exit now: %s" % e)

    finally:
        # always clean up the session, no matter if we caught an exception or
        # not.
        print("closing session")
        session.close ()

        # the above is equivalent to
        #
        #   session.close (cleanup=True, terminate=True)
        #
        # it will thus both clean out the session's database record, and kill
        # all remaining pilots (none in our example).


# -------------------------------------------------------------------------------

