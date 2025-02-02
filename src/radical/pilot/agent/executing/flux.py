
__copyright__ = 'Copyright 2013-2020, http://radical.rutgers.edu'
__license__   = 'MIT'


import time
import queue
import threading as mt

import radical.utils as ru

from ...   import states    as rps
from ...   import constants as rpc

from ..    import LaunchMethod
from ..    import ResourceManager
from .base import AgentExecutingComponent


# ------------------------------------------------------------------------------
#
class Flux(AgentExecutingComponent) :

    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg, session):

        AgentExecutingComponent.__init__(self, cfg, session)


    # --------------------------------------------------------------------------
    #
    def initialize(self):
        '''
        This components has 3 strands of activity (threads):

          - the main thread listens for incoming tasks from the scheduler, and
            pushes them toward the watcher thread;
          - an event listener thread listens for flux events which signify task
            state updates, and pushes those events also to the watcher thread;
          - the watcher thread matches events and tasks, enacts state updates,
            and pushes completed tasks toward output staging.

        NOTE: we get tasks in *AGENT_SCHEDULING* state, and enact all
              further state changes in this component.
        '''

        # translate Flux states to RP states
        self._event_map = {'NEW'     : None,   # rps.AGENT_SCHEDULING,
                           'DEPEND'  : None,
                           'SCHED'   : rps.AGENT_EXECUTING_PENDING,
                           'RUN'     : rps.AGENT_EXECUTING,
                           'CLEANUP' : None,
                           'INACTIVE': rps.AGENT_STAGING_OUTPUT_PENDING,
                           'PRIORITY': None,
                          }

        # we get an instance of the resource manager (init from registry info)
        self._rm = ResourceManager.create(name=self._cfg.resource_manager,
                                          cfg=self._cfg, log=self._log,
                                          prof=self._prof)

      # assert self._rm.from_info


        # thread termination signal
        self._term = mt.Event()

        # need two queues, for tasks and events
        self._task_q  = queue.Queue()
        self._event_q = queue.Queue()

        # run listener thread
        self._listener_setup  = mt.Event()
        self._listener        = mt.Thread(target=self._listen)
        self._listener.daemon = True
        self._listener.start()

        # run watcher thread
        self._watcher_setup  = mt.Event()
        self._watcher        = mt.Thread(target=self._watch)
        self._watcher.daemon = True
        self._watcher.start()

        # main thread waits for tasks to arrive from the scheduler
        self.register_input(rps.AGENT_SCHEDULING,
                            rpc.AGENT_EXECUTING_QUEUE, self.work)

        # wait for some time to get watcher and listener initialized
        start = time.time()
        while time.time() - start < 10.0:
            if self._watcher_setup.is_set() and \
               self._listener_setup.is_set():
                break

        assert self._watcher_setup.is_set()
        assert self._listener_setup.is_set()


    # --------------------------------------------------------------------------
    #
    def cancel_task(self, uid):

        # FIXME: clarify how to cancel tasks in Flux
        pass


    # --------------------------------------------------------------------------
    #
    def work(self, tasks):

        self._task_q.put(ru.as_list(tasks))

        if self._term.is_set():
            self._log.warn('threads triggered termination')
            self.stop()


    # --------------------------------------------------------------------------
    #
    def _listen(self):

        lm_cfg  = self._cfg.resource_cfg.launch_methods.get('FLUX')
        lm_cfg['pid']       = self._cfg.pid
        lm_cfg['reg_addr']  = self._cfg.reg_addr
        lm                  = LaunchMethod.create('FLUX', lm_cfg, self._cfg,
                                                  self._log, self._prof)
        flux_handle = None
        try:

            flux_handle = lm.fh.get_handle()
            flux_handle.event_subscribe('job-state')

            # FIXME: how tot subscribe for task return code information?
            # pylint: disable=import-error
          # def _flux_cb(self, *args, **kwargs):
          #     print('--------------- flux cb %s' % [args, kwargs])
          #
          # from flux import future as flux_future
          # fh = self._flux.flux_job_event_watch(jobid, 'eventlog', 0)
          # f  = flux_future.Future(fh)
          # f.then(_flux_cb)


            # signal successful setup to main thread
            self._listener_setup.set()

            while True:

                # FIXME: how can recv be timed out or interrupted after work
                #        completed?
                event = flux_handle.event_recv()

                if 'transitions' not in event.payload:
                    self._log.warn('unexpected flux event: %s' %
                                    event.payload)
                    continue

                transitions = ru.as_list(event.payload['transitions'])

                self._event_q.put(transitions)


        except Exception:

            if flux_handle:
                flux_handle.event_unsubscribe('job-state')

            self._log.exception('Error in listener loop')
            self._term.set()


    # --------------------------------------------------------------------------
    #
    def handle_events(self, task, events):
        '''
        Return `True` on final events so that caller can clean caches.
        Note that this relies on Flux events to arrive in order
        (or at least in ordered bulks).
        '''

        ret = False

        for event in events:

            flux_state = event[1]  # event: flux_id, flux_state
            state = self._event_map.get(flux_state)

            if state is None:
                # ignore this state transition
                self._log.debug('ignore flux event %s:%s' %
                                (task['uid'], flux_state))

            # FIXME: how to get actual event transition timestamp?
          # ts = event.time
            ts = time.time()

            if state == rps.AGENT_STAGING_OUTPUT_PENDING:
                task['target_state'] = rps.DONE  # FIXME
                # on completion, push toward output staging
                self.advance_tasks(task, state, ts=ts, publish=True, push=True)
                ret = True

            else:
                # otherwise only push a state update
                self.advance_tasks(task, state, ts=ts, publish=True, push=False)

        return ret


    # --------------------------------------------------------------------------
    #
    def _watch(self):

        try:

            # thread local initialization
            tasks  = dict()
            events = dict()

            self.register_output(rps.AGENT_STAGING_OUTPUT_PENDING,
                                 rpc.AGENT_STAGING_OUTPUT_QUEUE)

            # signal successful setup to main thread
            self._watcher_setup.set()

            while not self._term.is_set():

                active = False
                tasks  = list()
                events = list()


                try:
                    for task in self._task_q.get_nowait():
                        tasks.append(task)

                except queue.Empty:
                    # nothing found -- no problem, check if we got some events
                    pass

                for task in tasks:

                    active = True
                    try:

                        flux_id = task['flux_id']
                        assert flux_id not in tasks
                        tasks[flux_id] = task

                        # handle and purge cached events for that task
                        if flux_id in events:
                            if self.handle_events(task, events[flux_id]):
                                # task completed - purge data
                                # NOTE: this assumes events are ordered
                                if flux_id in events: del events[flux_id]
                                if flux_id in tasks : del tasks[flux_id]

                    except Exception as e:

                        self._log.exception("error collecting Task")
                        task['exception']        = repr(e)
                        task['exception_detail'] = \
                                         '\n'.join(ru.get_exception_trace())

                        # can't rely on the executor base to free the
                        # task resources
                        self._prof.prof('unschedule_start', uid=task['uid'])
                        self.publish(rpc.AGENT_UNSCHEDULE_PUBSUB, task)

                        self.advance_tasks(task, rps.FAILED, publish=True,
                                                             push=False)


                try:
                    for event in self._event_q.get_nowait():
                        events.append(event)
                except queue.Empty:
                    # nothing found -- no problem, check if we got some tasks
                    pass

                for event in events:

                    active = True
                    flux_id = event[0]  # event: flux_id, flux_state

                    if flux_id in tasks:

                        # known task - handle events
                        if self.handle_events(tasks[flux_id], [event]):
                            # task completed - purge data
                            # NOTE: this assumes events are ordered
                            if flux_id in events: del events[flux_id]
                            if flux_id in tasks : del tasks[flux_id]

                    else:
                        # unknown task, store events for later
                        if flux_id not in events:
                            events[flux_id] = list()
                        events[flux_id].append(event)


                if not active:
                    time.sleep(0.01)

        except Exception:
            self._log.exception('Error in watcher loop')
            self._term.set()


# ------------------------------------------------------------------------------

