_author__    = "George Chantzialexiou"
__copyright__ = "Copyright  2012-2013, The Pilot Project"
__license__   = "MIT"

import os, sys,  radical.pilot, math  # , multiprocessing
from random import randint
import time



"""
    This is a simple impementation of k-means algorithm
    using the Radical-Pilot API.


"""
# DBURL defines the MongoDB server URL and has the format mongodb://host:port.
# For the installation of a MongoDB server, refer to http://docs.mongodb.org.
DBURL = os.getenv("RADICAL_PILOT_DBURL")
if DBURL is None:
    print "ERROR: RADICAL_PILOT_DBURL (MongoDB server URL) is not defined."
    sys.exit(1)

#------------------------------------------------------------------------------
#
def pilot_state_cb(pilot, state):
    """pilot_state_change_cb() is a callback function. It gets called very
    time a ComputePilot changes its state.
    """

    if state == radical.pilot.states.FAILED:
        print "Compute Pilot '%s' failed, exiting ..." % pilot.uid
        sys.exit(1)

    elif state == radical.pilot.states.ACTIVE:
        print "Compute Pilot '%s' became active!" % (pilot.uid)

#------------------------------------------------------------------------------
#
def unit_state_change_cb(unit, state):
    """unit_state_change_cb() is a callback function. It gets called very
    time a ComputeUnit changes its state.
    """
    if state == radical.pilot.states.FAILED:
        print "Compute Unit '%s' failed ..." % unit.uid
        sys.exit(1)

    elif state == radical.pilot.states.DONE:
        print "Compute Unit '%s' finished with output:" % (unit.uid)
        print unit.stdout

#------------------------------------------------------------------------------
#
def quickselect(x, k):    
    # k should start from 0
    # returns the element itself
     
    pivot = x[len(x) // 2]
    left = [e for e in x if e < pivot]
    right = [e for e in x if e > pivot]
    
    delta = len(x) - len(right)
    if k < len(left):
        return quickselect(left, k)
    elif k >= delta:
        return quickselect(right, k - delta)
    else:
        return pivot
#-------------------------------------------------------------------------
#
def get_distance(dataPointX, dataPointY, centroidX, centroidY):
    
    # Calculate Euclidean distance.
    return math.sqrt(math.pow((centroidY - dataPointY), 2) + math.pow((centroidX - dataPointX), 2))
#-------------------------------------------------------------------------
# 

def main():
    try:
    	start_time = time.time()   
        #----------FUNCTIONS OF RADICAL PILOT -------------------#
        # here we create a new radical session
        #DBURL = "mongodb://localhost:27017"
        try:
            session = radical.pilot.Session(database_url = DBURL)
        except Exception, e:
            print "An error with mongodb has occured: %s" % (str(e))
            return (-1)

        # don't forget to change the localhost label
        #c = radical.pilot.Context('ssh')
        #c.user_id = 'userid'
        #session.add_context(c)
        
        # Add a Pilot Manager. Pilot managers manage one or more ComputePilots.
        print "Initiliazing Pilot Manager..."
        pmgr = radical.pilot.PilotManager(session=session)

        # Register our callback with our Pilot Manager. This callback will get
        # called every time any of the pilots managed by the PilotManager
        # change their state
        pmgr.register_callback(pilot_state_cb)

        # this describes the requirements and the paramers
        pdesc = radical.pilot.ComputePilotDescription()
        pdesc.resource =   "localhost"   #"sierra.futuregrid.org"
        pdesc.runtime = 10 # minutes 
        pdesc.cores =  4 # multiprocessing.cpu_count() # we use all the cores we have
        #pdesc.cleanup = True  # delete all the files that are created automatically when the job is done

        print "Submitting Compute Pilot to PilotManager"
        pilot = pmgr.submit_pilots(pdesc)

        umgr = radical.pilot.UnitManager(session=session, scheduler = radical.pilot.SCHED_DIRECT_SUBMISSION)

        # Combine all the units
        print "Initiliazing Unit Manager"

        # Combine the ComputePilot, the ComputeUnits and a scheduler via
        # a UnitManager object.
        umgr = radical.pilot.UnitManager(
            session=session,
            scheduler=radical.pilot.SCHED_DIRECT_SUBMISSION)

        # Register our callback with the UnitManager. This callback will get
        # called every time any of the units managed by the UnitManager
        # change their state.
        print 'Registering the callbacks so we can keep an eye on the CUs'
        umgr.register_callback(unit_state_change_cb)

        print "Registering Compute Pilot with Unit Manager"
        umgr.add_pilots(pilot) 
        # need to read the number of the k division the user want to do: that's also the only argument
        args = sys.argv[1:]
        if len(args) < 1:
            print "Usage: python %s needs the k-variable. Please run again" % __file__
            print "python k-means k"
            sys.exit(-1)
        k = int(sys.argv[1])  # k is the number of clusters i want to create
        # read the dataset from dataset.data file and pass the elements to x list
        try:
            data = open("dataset4.in",'r')
        except IOError:
            print "Missing dataset. file! Run:"
            sys.exit(-1)

        dataset_as_string_array = data.readline().split(',')
        x = map(float, dataset_as_string_array)
        data.close()
        #initialize the centroids   : choose the centroids and delete them from the element list (x)
        #----------FUNCTIONS OF RADICAL PILOT -------------------#


        #-------- CHOOSING THE CENTROIDS -----------#
        centroid = []
        size = len(x)
        for i in range(0,k):
            a1 = quickselect(x,(size*(i+1))//((k+1)))
            centroid.append(a1)

        for i in range(0,k):
            x.remove(centroid[i])
        #--------END OF CHOOSING CENTROIDS----------#
        
        #------------ PUT THE CENTROIDS IN A FILE -------------------#
        centroid_to_string = ','.join(map(str,centroid))
        centroid_file = open('centroidss.txt', 'w')
        centroid_file.write(centroid_to_string)
        centroid_file.close()       
        #------------END OF PUTTING CENTROIDS IN A FILE CENTROIDS.DATA ------#


        #-----------VARIABLE DEFINITIONS -------------------------------#
        p =   pdesc.cores  # NUMBER OF CORES OF THE SYSTEM I USE
        convergence = False   # We have no convergence yet
        m = 0 # nubmer of iterations
        maxIt = 20 # the maximum number of iteration
        part_length = len(x)/p  # this is the length of the part that each unit is going to control
        #----------END OF VARIABLE DEFINITIONS--------------------------#


        while ((m<maxIt) and (convergence==False)):
            #------------ PUT THE CENTROIDS INTO DIFFERENT FILES---------------#
            for i in range(1,p+1):
                input_file = open("cu_%d.data" % i, "w")
                p1 = part_length*(i-1)
                p2 = part_length*(i) 
                if (i==p):
                    p2 = len(x)
                input_string = ','.join(map(str,x[p1:p2]))
                input_file.write(input_string)
                input_file.close()
            #--------------END OF PUTTING CENTROIDS INTO DIFFERENT FILES---------#
            
            
            #------------------GIVE THE FILES INTO CUS TO START CALCULATING THE CLUSTERS - PHASE A -------------------#
            mylist = []
            for i in range(1,p+1):
                cudesc = radical.pilot.ComputeUnitDescription()    
                cudesc.environment = {"cu": "%d" % i, "k": "%d" % k}
                cudesc.executable  = "python"
                cudesc.arguments = ['clustering_the_elements.py','$cu','$k']    
                cudesc.input_data = ['clustering_the_elements.py', 'cu_%d.data' % i,'centroidss.txt']
                cudesc.output_data = []
                file_string = 'centroid_cu_%d.data' % i
                cudesc.output_data.append(file_string)
                mylist.append(cudesc)
                
            print 'Submitting the CU to the Unit Manager...'
            mylist_units = umgr.submit_units(mylist)
            # wait for all units to finish
            umgr.wait_units()
            print "All Compute Units completed PhaseA successfully! Now.." 
            #print " We compose the files into k files "

            #--------------------END OF PHASE A - NOW WE HAVE THE CLUSTERS CALCULATED IN THE CU FILES --------------------------#


            #------------------FINDING THE AVG_ELEMENTS WHICH ARE CANDIATE CENTROIDS -----------------------#

            sum_of_all_centroids = []
            for i in range(0,2*k):
            	sum_of_all_centroids.append(0)
            
            for i in range(1,p+1):
                read_file = open("centroid_cu_%d.data" % i, "r")
                read_as_string_array = read_file.readline().split(',')
                read_as_float_array = map(float, read_as_string_array)
                for j in range(0,k):
	                sum_of_all_centroids[2*j] += read_as_float_array[2*j]
	            	sum_of_all_centroids[(2*j)+1] += read_as_float_array[(2*j)+1]
            	read_file.close()

            for i in range(0,k):
                if (sum_of_all_centroids[(2*i)+1] != 0):
                    sum_of_all_centroids[i] = sum_of_all_centroids[(2*i)]/sum_of_all_centroids[(2*i)+1]
                else:
                    sum_of_all_centroids[i] = -1   #there are no centroids in this cluster

            # writing the elements to a file
            input_file = open('centroidss.txt','w')
            input_string = ','.join(map(str,sum_of_all_centroids[0:p])) 
            input_file.write(input_string)
            input_file.close()


            #------------------END THE AVG_ELEMENTS WHICH ARE CANDIATE CENTROIDS  -----------------------#
            mylist = []
            cudesc.output_data = []
            
            for i in range(1,p+1):
                cudesc = radical.pilot.ComputeUnitDescription()    
                cudesc.environment = {"cu": "%d" % i, "k": "%d" % k}
                cudesc.executable  = "python"
                cudesc.arguments = ['finding_the_new_centroids.py','$cu','$k']    
                cudesc.input_data = ['finding_the_new_centroids.py', 'cu_%d.data' % i,'centroidss.txt']
                cudesc.output_data = []
                file_string = 'centroid_cu_%d.data' % i
                cudesc.output_data.append(file_string)
                mylist.append(cudesc)
                
            print 'Submitting the CU to the Unit Manager...'
            mylist_units = umgr.submit_units(mylist)
            # wait for all units to finish
            umgr.wait_units()
            print "All Compute Units completed PhaseB successfully! Now.." 
            #print " We compose the files into k files "

            
            #-------FINDING THE NEW CENTROIDS- THESE ARE THE ELEMENTS WHO ARE CLOSER TO THE AVG_ELEMENTS-----#
            	# sum_of_all_centroids have the avg_elemnt
            new_centroids = []
            a = sys.maxint
            for i in range(0,k):
                new_centroids.append(a)
            
            for i in range(1,p+1):
            	read_file = open("centroid_cu_%d.data" % i, "r")
            	read_as_string_array = read_file.readline().split(',')
            	read_as_float_array = map(float,read_as_string_array)
            	for j in range(0,k):
            		if get_distance(new_centroids[j],0,sum_of_all_centroids[j],0) > get_distance(read_as_float_array[j],0,sum_of_all_centroids[j],0):
            			new_centroids[j] = read_as_float_array[j]
            	read_file.close()


            #-----END OF FINDING THE NEW CENTROIDS- THESE ARE THE ELEMENTS WHO ARE CLOSER TO THE AVG_ELEMENT---#
         

            #-------------------CHECKING FOR CONVERGENCE------------------------------------#
            
            # now we check for convergence - the prev centroids are in !centroid! and the new are in !new_centroids! 
            print "Now we check the converge"
            print 'new centroids:'
            print new_centroids
            print 'Old centroids:'
            print centroid
           # print 'element list'
           # print x
            convergence = True
            for i in range(0,len(new_centroids)):
                if (abs(new_centroids[i] - centroid[i]) > 1000): # i have elements from 1 to 10.000 so I give 1000 convergence
                    convergence = False

            if (convergence == False):
                m +=1
                for l in range(0,k):   # put the old centroids to the element file... remove the new cetroids from element list
                    if (centroid[l] != new_centroids[l]):
                        x.remove(new_centroids[l])
                        x.append(centroid[l])
                centroid =new_centroids
                input_string = ','.join(map(str,new_centroids))
                input_file = open('centroidss.txt', 'w')
                input_file.write(input_string)
                input_file.close()
            
            #------------------END OF CHECKING FOR CONVERGENCE-----------------------------#

        print 'K-means algorithm ended successfully after %d iterations' % m
        # the only thing i have to do here is to check for convergence & add the file- arguments into the enviroments
        #print 'Thre centroids are in the cetroidss.txt file, and the elements of each centroid at centroid_x.data file'
        print 'The centroids of these elements are: \n'
        print new_centroids
        
        #--------------------END OF K-MEANS ALGORITHM --------------------------#
        finish_time = time.time()
        total_time = finish_time - start_time  # total execution time
        print 'The total execution time is: %f seconds' % total_time
        total_time /= 60
        print 'Which is: %f minutes' % total_time
        session.close()
        print "Session closed, exiting now ..."
        sys.exit(0)


    except Exception, e:
        print "AN ERROR OCCURRED: %s" % ((str(e)))
        return(-1)


#------------------------------------------------------------------------------
#
if __name__ == "__main__":
    sys.exit(main())

#
#------------------------------------------------------------------------------
