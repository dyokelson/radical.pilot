#!/bin/bash
#!/bin/bash

#SBATCH -J rp_integration_test          # job name
#SBATCH -o rp_integration_test.%j.out   # stdout file (%j expands to jobId)
#SBATCH -e rp_integration_test.%j.err   # stderr file (%j expands to jobId)
#SBATCH -p compute                      # partition to use
#SBATCH -N 1                            # total number of nodes (16 cores/node)
#SBATCH -n 1                            # total number of mpi tasks requested
#SBATCH -t 00:30:00                     # run time (hh:mm:ss) - 0.5 hours
#SBATCH -A                              # allocation to charge job against
#SBATCH -C EGRESS

# ------------------------------------------------------------------------------
# Test files
TEST_SLURM='radical.pilot/tests/integration_tests/test_rm/test_slurm.py'
TEST_ISSUE='radical.pilot/tests/bin/radical-pilot-test-issue'

# ------------------------------------------------------------------------------
# Test folder, the same as the sbatch script submit folder
cd $SLURM_SUBMIT_DIR
rm -rf radical.pilot testing *.log
git clone --branch devel https://github.com/radical-cybertools/radical.pilot.git

# ------------------------------------------------------------------------------
# Python distribution specific. Change if needed.
module load python3conda/3.8.5
conda create -p testing python=3.7 pytest PyGithub -y
source activate $PWD/testing
tmpLOC=`find $PWD/lib -name "site-packages"`/
PYTHONPATH=$tmpLOC:$PYTHONPATH

# ------------------------------------------------------------------------------
# Test execution
pip install ./radical.pilot --upgrade
pytest -vvv $TEST > output.log 2>&1

if test "$?" = 1
then
    $TEST_ISSUE -r 'SDSC Comet' -l output.log
    sbatch --begin='now+4weeks' comet.sh 
else
    sbatch --begin='now+1week' comet.sh 
fi
