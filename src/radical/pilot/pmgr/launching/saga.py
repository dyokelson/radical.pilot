# pylint: disable=protected-access

__copyright__ = 'Copyright 2022, The RADICAL-Cybertools Team'
__license__   = 'MIT'


import os
import functools

import threading     as mt
import radical.utils as ru

# saga is optional
rs    = None
rs_ex = None

try:
    import radical.saga
    rs = radical.saga
except ImportError as ex:
    rs_ex = ex

from .base import PilotLauncherBase
from ...   import states as rps


# ------------------------------------------------------------------------------
# local constants

PILOT_CHECK_INTERVAL    =  60  # seconds between runs of the job state check loop

# FIXME: duplication from base class, use `self._cfg`

PILOT_CANCEL_DELAY      = 120  # seconds between cancel signal and job kill
PILOT_CHECK_MAX_MISSES  =   3  # number of times to find a job missing before
                               # declaring it dead


# ------------------------------------------------------------------------------
#
class PilotLauncherSAGA(PilotLauncherBase):

    # --------------------------------------------------------------------------
    #
    def __init__(self, name, log, prof, state_cb):

        if rs_ex:
            raise rs_ex

        assert(rs)

        PilotLauncherBase.__init__(self, name, log, prof, state_cb)

        self._saga_jobs = dict()      # pid      : rs.Job
        self._saga_js   = dict()      # resource : rs.JobService
        self._pilots    = dict()      # saga_id  : pilot job
        self._saga_lock = mt.RLock()  # lock for above


        # FIXME: get session from launching component
        self._saga_session = rs.Session()

        # FIXME: make interval configurable


    # --------------------------------------------------------------------------
    #
    def _translate_state(self, saga_state):

        if   saga_state == rs.job.NEW       : return rps.NEW
        elif saga_state == rs.job.PENDING   : return rps.PMGR_LAUNCHING
        elif saga_state == rs.job.RUNNING   : return rps.PMGR_LAUNCHING
        elif saga_state == rs.job.SUSPENDED : return rps.PMGR_LAUNCHING
        elif saga_state == rs.job.DONE      : return rps.DONE
        elif saga_state == rs.job.FAILED    : return rps.FAILED
        elif saga_state == rs.job.CANCELED  : return rps.CANCELED
        else:
            raise ValueError('cannot interpret psij state: %s' % saga_state)


    # --------------------------------------------------------------------------
    #
    def _job_state_cb(self, job, _, saga_state, pid):

        try:
            with self._saga_lock:

                if job.id not in self._pilots:
                    return

                rp_state = self._translate_state(saga_state)
                pilot    = self._pilots[pid]

            self._state_cb(pilot, rp_state)

        except Exception:
            self._log.exception('job status callback failed')

        return True


    # --------------------------------------------------------------------------
    #
    def finalize(self):

        # FIXME: terminate thread

        with self._saga_lock:

            # cancel pilots
            for _, job in self._saga_jobs:
                job.cancel()

            # close job services
            for url, js in self._saga_js.items():
                self._log.debug('close js %s', url)
                js.close()


    # --------------------------------------------------------------------------
    #
    def can_launch(self, rcfg, pilot):

        # SAGA can launch all pilots
        return True


    # --------------------------------------------------------------------------
    #
    def launch_pilots(self, rcfg, pilots):

        js_ep  = rcfg['job_manager_endpoint']
        with self._saga_lock:
            if js_ep in self._saga_js:
                js = self._saga_js[js_ep]
            else:
                js = rs.job.Service(js_ep, session=self._saga_session)
                self._saga_js[js_ep] = js

        # now that the scripts are in place and configured,
        # we can launch the agent
        jc = rs.job.Container()

        for pilot in pilots:

            jd_dict = pilot['jd_dict']

            saga_jd_supplement = dict()
            if 'saga_jd_supplement' in jd_dict:
                saga_jd_supplement = jd_dict['saga_jd_supplement']
                del(jd_dict['saga_jd_supplement'])


            jd = rs.job.Description()
            for k, v in jd_dict.items():
                jd.set_attribute(k, v)

            # set saga_jd_supplement if not already set
            for key, val in saga_jd_supplement.items():
                if not jd[key]:
                    self._log.debug('supplement %s: %s', key, val)
                    jd[key] = val

            # remember the pilot
            pid = pilot['uid']
            self._pilots[pid] = pilot

            job = js.create_job(jd)
            cb  = functools.partial(self._job_state_cb, pid=pid)
            job.add_callback(rs.STATE, cb)
            jc.add(job)

        jc.run()

        # Order of tasks in `rs.job.Container().tasks` is not changing over the
        # time, thus it's able to iterate over it and other list(s) all together
        for j, pilot in zip(jc.get_tasks(), pilots):

            # do a quick error check
            if j.state == rs.FAILED:
                self._log.error('%s: %s : %s : %s', j.id, j.state, j.stderr, j.stdout)
                raise RuntimeError("SAGA Job state is FAILED. (%s)" % j.name)

            pid = pilot['uid']

            # FIXME: update the right pilot
            with self._saga_lock:
                self._saga_jobs[pid] = j


    # --------------------------------------------------------------------------
    #
    def _cancel_pilots(self, pids):

        tc = rs.job.Container()

        for pid in pids:

            job   = self._saga_jobs[pid]
            pilot = self._pilots[pid]['pilot']

            # don't overwrite resource_details from the agent
            if 'resource_details' in pilot:
                del(pilot['resource_details'])

            if pilot['state'] in rps.FINAL:
                continue

            self._log.debug('plan cancellation of %s : %s', pilot, job)
            self._log.debug('request cancel for %s', pilot['uid'])
            tc.add(job)

        self._log.debug('cancellation start')
        tc.cancel()
        tc.wait()
        self._log.debug('cancellation done')


# ------------------------------------------------------------------------------

