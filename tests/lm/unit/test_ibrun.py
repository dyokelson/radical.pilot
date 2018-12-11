
# pylint: disable=protected-access, unused-argument


from   test_common                   import setUp
from   radical.pilot.agent.lm.ibrun import IBRun

import radical.utils as ru
import pytest


try:
    import mock
except ImportError:
    from unittest import mock


# ------------------------------------------------------------------------------
#
@mock.patch.object(IBRun, '__init__',   return_value=None)
@mock.patch.object(IBRun, '_configure', return_value=None)
@mock.patch('radical.utils.raise_on')
def test_construct_command(mocked_init, 
                           mocked_configure,
                           mocked_raise_on):

    test_cases = setUp('lm', 'ibrun')
    component  = IBRun(name=None, cfg=None, session=None)

    component._log           = ru.get_logger('dummy')
    component.name           = 'IBRun'
    component.mpi_flavor     = None
    component.launch_command = 'ibrun'
    component.ccmrun_command = ''
    component.dplace_command = ''

    for unit, result in test_cases:
        if result == "RutimeError":
            with pytest.raises(RuntimeError):
                command, hop = component.construct_command(unit, None)
        else:
            command, hop = component.construct_command(unit, None)
            print command, hop
            assert([command, hop] == result)
            


# ------------------------------------------------------------------------------



