#!/usr/bin/env python3
"""
Executes state tests on multiple clients, checking for EVM trace equivalence

"""
import json, sys, re, os, subprocess, io, itertools, traceback, time, collections
from contextlib import redirect_stderr, redirect_stdout
import ethereum.transactions as transactions
from ethereum.utils import decode_hex, parse_int_or_hex, sha3, to_string, \
    remove_0x_head, encode_hex, big_endian_to_int

from evmlab import genesis as gen
from evmlab import vm as VMUtils
from evmlab import opcodes

import logging
logger = logging.getLogger()
logger.setLevel(logging.DEBUG)

ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
ch.setFormatter(formatter)
logger.addHandler(ch)

cfg ={}
local_cfg = {}

def parse_config():
    """Parses 'statetests.ini'-file, which 
    may contain user-specific configuration
    """

    import configparser, getpass
    config = configparser.ConfigParser()
    config.read('statetests.ini')
    uname = getpass.getuser()
    if uname not in config.sections():
        uname = "DEFAULT"

    cfg['RANDOM_TESTS'] = config[uname]['random_tests']
    cfg['DO_CLIENTS']  = config[uname]['clients'].split(",")
    cfg['FORK_CONFIG'] = config[uname]['fork_config']
    cfg['TESTS_PATH']  = config[uname]['tests_path']

    global local_cfg

    local_cfg = collections.defaultdict(lambda: None, config[uname])
    print(local_cfg["geth.binary"])
    print(local_cfg["test"])
    # Make it possible to run in paralell sessions    
    cfg['PRESTATE_TMP_FILE']    ="%s-%d" % (config[uname]['prestate_tmp_file'] , os.getpid())
    cfg['SINGLE_TEST_TMP_FILE'] ="%s-%d" % (config[uname]['single_test_tmp_file'], os.getpid())

    cfg['LOGS_PATH'] = config[uname]['logs_path']

    logger.info("Config")
    logger.info("\tActive clients:")
    for c in cfg['DO_CLIENTS']:
        logger.info("\t* {} : {} docker:{}".format(c, getBaseCmd(c)[0],getBaseCmd(c)[1]) )

    logger.info("\tTest generator:")
    logger.info("\t* {} : {} docker:{}".format('testeth', getBaseCmd('testeth')[0],getBaseCmd('testeth')[1]) )
 
    logger.info("\tFork config:          %s",         cfg['FORK_CONFIG'])
    logger.info("\tPrestate tempfile:    %s",   cfg['PRESTATE_TMP_FILE'])
    logger.info("\tSingle test tempfile: %s",cfg['SINGLE_TEST_TMP_FILE'])
    logger.info("\tLog path:             %s",            cfg['LOGS_PATH'])



def getBaseCmd(bin_or_docker):
    """ Gets the configured 'base_command' for an image or binary. 
    Returns a path or image name and a boolean if it's a docker 
    image or not (needed to know if any mounts are needed)
    returns a tuple: ( name  , isDocker)
    """

    binary = local_cfg["{}.binary".format(bin_or_docker) ]
    if binary:
        return (binary, False)

    image = local_cfg["{}.docker_name".format(bin_or_docker)]
    if image: 
        return (image, True)

        
parse_config()


class GeneralTest():

    def __init__(self, json_data, filename):

        self.fork_name = cfg['FORK_CONFIG']
        
        self.subfolder = filename.split(os.sep)[-2]
        # same default as evmlab/genesis.py
        metroBlock = 2000
        if self.fork_name == 'Byzantium':
            metroBlock = 0
        
        self.json_data = json_data

    def individual_tests(self):

        fork_under_test = self.fork_name

        prestate = {
            'config' : { # for pyeth run_statetest.py
                'metropolisBlock' : 2000, # same default as evmlab/genesis.py
                'eip158Block' : 2000,
                'eip150Block' : 2000,
                'eip155Block' : 2000,
                'homesteadBlock' : 2000,
            }
        }
        if fork_under_test == 'Byzantium':
            prestate['config'] = {
                'metropolisBlock' : 0,
                'eip158Block' : 0,
                'eip150Block' : 0,
                'eip155Block' : 0,
                'homesteadBlock' : 0,
            }

        if fork_under_test == 'Homestead':
            prestate['config']['homesteadBlock'] = 0

        json_data = self.json_data

        for test_name in json_data:
            prestate['env'] = json_data[test_name]['env']
            prestate['pre'] = json_data[test_name]['pre']

            general_tx = json_data[test_name]['transaction']
            
            tx_i = 0
            for poststate in json_data[test_name]['post'][fork_under_test]:
                
                poststate = poststate.copy()

                tx = general_tx.copy()
                d = poststate['indexes']['data']
                g = poststate['indexes']['gas']
                v = poststate['indexes']['value']
                tx['data'] = [general_tx['data'][d]]
                tx['gasLimit'] = [general_tx['gasLimit'][g]]
                tx['value'] = [general_tx['value'][v]]
                
                single_test = json_data.copy()
                poststate['indexes'] =  {'data':0,'gas':0,'value':0}
                single_test[test_name]['post'] = { fork_under_test: [ poststate ] }
                single_test[test_name]['transaction'] = tx
 
                state_test = StateTest()

                state_test.subfolder = self.subfolder
                state_test.name = test_name
                state_test.tx_i = tx_i
                state_test.statetest = single_test
                state_test.tx = tx
                state_test.tx_dgv = (d,g,v)

                tx_i = tx_i +1


                yield state_test

class StateTest():
    """ This class represents a single statetest, with a single post-tx result: one transaction
    executed on one single fork
    """
    def __init__(self):
        self.number = None
        self.subfolder = None
        self.name = None
        self.tx_i = None
        self.statetest = None
        self.tx = None
        self.tx_dgv = None
        self.canon_traces = []
        self.procs = []
        self.traceFiles = []
        self.tmpfile = cfg['SINGLE_TEST_TMP_FILE']

    def id(self):
        return "{:0>4}-{}-{}-{}".format(self.number,self.subfolder,self.name,self.tx_i)

    def writeToFile(self):

        with open(self.tmpfile, 'w') as outfile:
            json.dump(self.statetest, outfile)



def iterate_tests(path = '/GeneralStateTests/', ignore = []):
    logging.info (cfg['TESTS_PATH'] + path)
    for subdir, dirs, files in sorted(os.walk(cfg['TESTS_PATH'] + path)):
        for f in files:
            if f.endswith('json'):
                for ignore_name in ignore:
                    if f.find(ignore_name) != -1:
                        continue
                    yield os.path.join(subdir, f)


def dumpJson(obj, dir = None, prefix = None):
    import tempfile
    fd, temp_path = tempfile.mkstemp(prefix = 'randomtest_', suffix=".json", dir = dir)
    with open(temp_path, 'w') as f :
        json.dump(obj,f)
        logger.info("Saved file to %s" % temp_path)
    os.close(fd)
    return temp_path

def createRandomStateTest():
    (name, isDocker) = getBaseCmd("testeth")
    if isDocker:
        cmd = ['docker', "run", "--rm",name]
    else:
        cmd = [name]

    cmd.extend(["-t","GeneralStateTests","--","--createRandomTest"])
    outp = "".join(VMUtils.finishProc(VMUtils.startProc(cmd)))
    #Validate that it's json
    try:
        test = json.loads(outp)
        test['randomStatetest']['_info'] = {'sourceHash': "0000000000000000000000000000000000000000000000000000000000001337", "comment":"x"}

        return test
    except:
        print("Exception generating test")
        print('-'*60)
        traceback.print_exc(file=sys.stdout)
        print('-'*60)
    return None


def generateTests():
    import getpass, time
    uname = getpass.getuser()
    host_id = "%s-%s-%d" % (uname, time.strftime("%a_%H_%M_%S"), os.getpid())
    here = os.path.dirname(os.path.realpath(__file__))

    cfg['TESTS_PATH'] = "%s/generatedTests/" % here
    # cpp needs the tests to be placed according to certain rules... 
    testfile_dir = "%s/generatedTests/GeneralStateTests/stRandom" % here
    filler_dir = "%s/generatedTests/src/GeneralStateTestsFiller/stRandom" % here 

    os.makedirs( testfile_dir , exist_ok = True)
    os.makedirs( filler_dir, exist_ok = True)
    import pathlib

    counter = 0
    while True: 
        test_json =  createRandomStateTest()
        if test_json == None: 
            time.sleep(2)
            continue

        identifier = "%s-%d" %(host_id, counter)
        test_fullpath = "%s/randomStatetest%s.json" % (testfile_dir, identifier)
        filler_fullpath = "%s/randomStatetest%sFiller.json" % (filler_dir, identifier)
        test_json['randomStatetest%s' % identifier] =test_json.pop('randomStatetest', None) 

        
        with open(test_fullpath, "w+") as f:
            json.dump(test_json, f)
            pathlib.Path(filler_fullpath).touch()

        yield test_fullpath
        counter = counter +1






TEST_WHITELIST = []


SKIP_LIST = [
    #'modexp_*', # regex example
    'POP_Bounds',
    'POP_BoundsOOG',
    'MLOAD_Bounds',
    'Call1024PreCalls', # Call1024PreCalls does produce a trace difference, worth fixing that trace
    'createInitFailStackSizeLargerThan1024',
    'createJS_ExampleContract',
    'CALL_Bounds',
    'mload32bitBound_Msize ',
    'mload32bitBound_return2',
    'Call1MB1024Calldepth ',
    'shallowStackOK',
    'stackOverflowM1PUSH', # slow
    'static_Call1MB1024Calldepth', # slow
    'static_Call1024BalanceTooLow',
    'static_Call1024BalanceTooLow2',
    'static_Call1024OOG',
    'static_Call1024PreCalls',
    'static_Call1024PreCalls2', # slow
    'static_Call1024PreCalls3', #slow
    'static_Call50000',
    'static_Call50000bytesContract50_1',
    'static_Call50000bytesContract50_2',
    'static_Call50000bytesContract50_3',
    'static_CallToNameRegistratorAddressTooBigLeft',
    'static_Call50000_identity2',
    'static_Call50000_identity',
    'static_Call50000_ecrec',
    'static_Call50000_rip160',
    'static_Call50000_sha256',
    'static_Return50000_2',
    'static_callChangeRevert',
    'static_log3_MaxTopic',
    'static_log4_Caller',
    'static_RawCallGas',
    'static_RawCallGasValueTransfer',
    'static_RawCallGasValueTransferAsk',
    'static_RawCallGasValueTransferMemory',
    'static_RawCallGasValueTransferMemoryAsk',
    'static_refund_CallA_notEnoughGasInCall',
    'static_LoopCallsThenRevert',
    'HighGasLimit', # geth doesn't run
    'zeroSigTransacrionCreate', # geth fails this one
    'zeroSigTransacrionCreatePrice0', # geth fails
    'zeroSigTransaction', # geth fails
    'zeroSigTransaction0Price', # geth fails
    'zeroSigTransactionInvChainID',
    'zeroSigTransactionInvNonce',
    'zeroSigTransactionInvNonce2',
    'zeroSigTransactionOOG',
    'zeroSigTransactionOrigin',
    'zeroSigTransactionToZero',
    'zeroSigTransactionToZero2',
    'OverflowGasRequire2',
    'TransactionDataCosts652',
    'stackLimitPush31_1023',
    'stackLimitPush31_1023',
    'stackLimitPush31_1024',
    'stackLimitPush31_1025', # test runner crashes
    'stackLimitPush32_1023',
    'stackLimitPush32_1024',
    'stackLimitPush32_1025', # big trace, onsensus failure
    'stackLimitGas_1023',
    'stackLimitGas_1024', # consensus bug
    'stackLimitGas_1025'
]

regex_skip = [skip.replace('*', '') for skip in SKIP_LIST if '*' in skip]

# to resume running after interruptions
START_I = 0




def randomTestIterator():

    number = 0

    for f in generateTests():

        with open(f) as json_data:
            general_test = GeneralTest(json.load(json_data),f)

        for state_test in general_test.individual_tests():
            state_test.number = number
            number = number +1
            yield state_test

def main():
    perform_tests(randomTestIterator)


def finishProc(name, processInfo, canonicalizer, fulltrace_filename = None):
    """ Ends the process, returns the canonical trace and also writes the 
    full process output to a file, along with the command used to start the process"""

    process = processInfo['proc']

    extraTime = False
    if name == "py":
        extraTime = True

    outp = VMUtils.finishProc(processInfo['proc'], extraTime, processInfo['output'])

    if fulltrace_filename is not None:
        #logging.info("Writing %s full trace to %s" % (name, fulltrace_filename))
        with open(fulltrace_filename, "w+") as f: 
            f.write("# command\n")
            f.write("# %s\n\n" % processInfo['cmd'])
            f.write("\n".join(outp))

    canon_text = [VMUtils.toText(step) for step in canonicalizer(outp)]
    return canon_text

def get_summary(combined_trace, n=20):
    """Returns (up to) n (default 20) preceding steps before the first diff, and the diff-section
    """
    from collections import deque
    buf = deque([],n)
    index = 0
    for index, line in enumerate(combined_trace):
        if line.startswith("[!!]"):
            buf.append("\n---- [ %d steps in total before diff ]-------\n\n" % (index))
            break
        buf.append(line)

    for i in range(index, min(len(combined_trace), index+5 )):
        buf.append(combined_trace[i])

    return list(buf)

def startGeth(test):

    testfile_path = os.path.abspath(test.tmpfile)
    mount_testfile = testfile_path + ":" + "/mounted_testfile"

    (name, isDocker) = getBaseCmd("geth")
    if isDocker:
        cmd = ["docker", "run", "--rm", "-t", "-v", mount_testfile, name, "--json", "--nomemory", "statetest", "/mounted_testfile"]
        return {'proc':VMUtils.startProc(cmd ), 'cmd': " ".join(cmd), 'output' : 'stdout'}
    else:
        cmd = [name,"--json", "--nomemory", "statetest", testfile_path]
        return {'proc':VMUtils.startProc(cmd ), 'cmd': " ".join(cmd), 'output' : 'stderr'}

def startParity(test):

    testfile_path = os.path.abspath(test.tmpfile)
    mount_testfile = testfile_path + ":" + "/mounted_testfile"

    (name, isDocker) = getBaseCmd("parity")
    if isDocker:
        cmd = ["docker", "run", "--rm", "-t", "-v", mount_testfile, name, "state-test", "/mounted_testfile", "--json"]
    else:
        cmd = [name,"state-test", testfile_path, "--json"]


    return {'proc':VMUtils.startProc(cmd ), 'cmd': " ".join(cmd), 'output' : 'stdout'}

def startCpp(test):

    [d,g,v] = test.tx_dgv


    (name, isDocker) = getBaseCmd("cpp")
    if isDocker:
        cpp_mount_tests = cfg['TESTS_PATH'] + ":" + "/mounted_tests"
        cmd = ["docker", "run", "--rm", "-t", "-v", cpp_mount_tests, name
                ,'-t',"GeneralStateTests/%s" %  test.subfolder
                ,'--'
                ,'--singletest', test.name
                ,'--jsontrace',"'{ \"disableStorage\":true, \"disableMemory\":true }'"
                ,'--singlenet',cfg['FORK_CONFIG']
                ,'-d',str(d),'-g',str(g), '-v', str(v)
                ,'--testpath', '"/mounted_tests"']
    else:
        cmd = [name
                ,'-t',"GeneralStateTests/%s" %  test.subfolder
                ,'--'
                ,'--singletest', test.name
                ,'--jsontrace',"'{ \"disableStorage\":true, \"disableMemory\":true }'"
                ,'--singlenet',cfg['FORK_CONFIG']
                ,'-d',str(d),'-g',str(g), '-v', str(v)
                ,'--testpath',  cfg['TESTS_PATH']]


    if cfg['FORK_CONFIG'] == 'Homestead' or cfg['FORK_CONFIG'] == 'Frontier':
        cmd.extend(['--all']) # cpp requires this for some reason

    return {'proc':VMUtils.startProc(cmd ), 'cmd': " ".join(cmd), 'output' : 'stdout'}

def startPython(test):

    tx_encoded = json.dumps(test.tx)
    tx_double_encoded = json.dumps(tx_encoded) # double encode to escape chars for command line

    prestate_path = os.path.abspath(test.prestate_tmpfile)
    mount_flag = prestate_path + ":" + "/mounted_prestate"
    cmd = ["docker", "run", "--rm", "-t", "-v", mount_flag, cfg['PYETH_DOCKER_NAME'], "run_statetest.py", "/mounted_prestate", tx_double_encoded]

    return {'proc':VMUtils.startProc(cmd), 'cmd': " ".join(cmd), 'output' : 'stdout'}


def start_processes(test):
    clients = cfg['DO_CLIENTS']

    starters = {'geth': startGeth, 'cpp': startCpp, 'py': startPython, 'parity': startParity}

    logger.info("Starting processes for %s on test %s" % ( clients, test.name))
    #Start the processes
    for client_name in clients:
        if client_name in starters.keys():
            procinfo = starters[client_name](test)
            test.procs.append( (procinfo, client_name ))        
        else:
            logger.warning("Undefined client %s", client_name)


canonicalizers = {
    "geth" : VMUtils.GethVM.canonicalized, 
    "cpp"  : VMUtils.CppVM.canonicalized, 
    "py"   : VMUtils.PyVM.canonicalized, 
    "parity"  :  VMUtils.ParityVM.canonicalized ,
}

def end_processes(test):
    # Handle the old processes
    if test is not None:
        for (procinfo, client_name) in test.procs:
            if procinfo['proc'] is None:
                continue

            canonicalizer = canonicalizers[client_name]
            full_trace_filename = os.path.abspath("%s/%s-%s.trace.log" % (cfg['LOGS_PATH'],test.id(), client_name))
            test.traceFiles.append(full_trace_filename)
            canon_trace = finishProc(client_name, procinfo, canonicalizer, full_trace_filename)

            test.canon_traces.append(canon_trace)

            logging.info("Processed %s steps for %s on test %s" % (len(canon_trace), client_name, test.name))


def processTraces(test):
    if test is None:
        return

    # Process previous traces
    (equivalent, trace_output) = VMUtils.compare_traces(test.canon_traces, cfg['DO_CLIENTS']) 

    if equivalent:
        #delete non-failed traces
        for f in test.traceFiles:
            os.remove(f)
    else:
        logger.warning("CONSENSUS BUG!!!")

        # save the state-test
        statetest_filename = "%s/%s-test.json" %(cfg['LOGS_PATH'], test.id())
        os.rename(test.tmpfile,statetest_filename)

        # save combined trace
        passfail_log_filename = "%s/FAIL-%s.log.txt" % ( cfg['LOGS_PATH'], test.id())

        with open(passfail_log_filename, "w+") as f:
            logger.info("Combined trace: %s" , passfail_log_filename)
            f.write("\n".join(trace_output))

        # save a summary of the trace, with up to 20 steps preceding the first diff
        trace_summary = get_summary(trace_output)
        summary_log_filename = "%s/FAIL-%s.summary.txt" % ( cfg['LOGS_PATH'],test.id())
        with open(summary_log_filename, "w+") as f:
            logger.info("Summary trace: %s" , summary_log_filename)
            f.write("\n".join(trace_summary))

        return equivalent

def perform_tests(test_iterator):

    pass_count = 0
    fail_count = 0
    failures = []

    previous_test = None

    start_time = time.time()

    n = 0
    for test in test_iterator():
        n = n+1
        #Prepare the current test
        logger.info("Test id: %s" % test.id())
        test.writeToFile()

        # Start new procs
        start_processes(test)

        # End previous procs
        traceFiles = end_processes(previous_test)


        # Process previous traces
        if processTraces(previous_test):
            pass_count = pass_count +1
        else:
            fail_count = fail_count +1

        # Do some reporting

        if n % 10 == 0:
            time_elapsed = time.time() - start_time
            logger.info("Fails: {}, Pass: {}, #test {} speed: {:f} tests/s".format(
                    fail_count, 
                    pass_count, 
                    (fail_count + pass_count),
                    (fail_count + pass_count) / time_elapsed
                ))
            break

        previous_test = test


    return (n, len(failures), pass_count, failures)

"""
## need to get redirect_stdout working for the python-afl fuzzer

# currently doPython() spawns a new process, and gets the pyethereum VM trace from the subprocess.Popen shell output.
# python-afl cannot instrument a separate process, so this prevents it from measuring the code/path coverage of pyeth

# TODO: invoke pyeth state test runner as a module (so python-afl can measure path coverage), and use redirect_stdout to get the trace


def runStateTest(test_case):
    _state = init_state(test_case['env'], test_case['pre'])
    f = io.StringIO()
    with redirect_stdout(f):
        computed = compute_state_test_unit(_state, test_case["transaction"], config_spurious)
    f.seek(0)
    py_out = f.read()
    print("py_out:", py_out)
"""
def testSummary():
    """Enable this, and test by passing a trace-output via console"""
    with open(sys.argv[1]) as f:
        print("".join(get_summary(f.readlines())))

if __name__ == '__main__':
#    testSummary()
    main()
