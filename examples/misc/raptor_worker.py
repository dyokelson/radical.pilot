
import time
import random

import radical.pilot as rp


# ------------------------------------------------------------------------------
#
class MyWorker(rp.raptor.MPIWorker):
    '''
    This class provides the required functionality to execute work requests.
    In this simple example, the worker only implements a single call: `hello`.
    '''

    class MyRankWorker(rp.raptor.RankWorker):

        def __init__(self, rank_task_q_get, rank_result_q_put, event,
                           log, prof, base):
            super().__init__(rank_task_q_get, rank_result_q_put, event,
                             log, prof, base)

            self.register_mode('foo', self._dispatch_foo)


        def _dispatch_foo(self, task):

            import pprint
            self._log.debug('==== running foo\n%s',
                    pprint.pformat(task['description']))

            return 'out', 'err', 0, None, None




    # --------------------------------------------------------------------------
    #
    def __init__(self, cfg):

        super().__init__(cfg)

    # --------------------------------------------------------------------------
    #
    def get_rank_worker(self):

        return self.MyRankWorker


    # --------------------------------------------------------------------------
    #
    def my_hello(self, uid, count=0):
        '''
        important work
        '''

        self._prof.prof('app_start', uid=uid)

        out = 'hello %5d @ %.2f [%s]' % (count, time.time(), self._uid)
        time.sleep(random.randint(1, 5))

        self._log.debug(out)

        self._prof.prof('app_stop', uid=uid)
        self._prof.flush()

        td = rp.TaskDescription({
                'mode'            : rp.TASK_EXECUTABLE,
                'scheduler'       : None,
                'ranks'           : 2,
                'executable'      : '/bin/sh',
                'arguments'       : ['-c',
                            'echo "hello $RP_RANK/$RP_RANKS: $RP_TASK_ID"']})

        td = rp.TaskDescription({
              # 'uid'             : 'task.call.w.000000',
              # 'timeout'         : 10,
                'mode'            : rp.TASK_EXECUTABLE,
                'ranks'           : 2,
                'executable'      : 'radical-pilot-hello.sh',
                'arguments'       : ['1', 'task.call.w.000000']})

        master = self.get_master()
        task   = master.run_task(td)

        print(task['stdout'])

        return 'my_hello retval'


# ------------------------------------------------------------------------------

