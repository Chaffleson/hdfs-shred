#!/usr/bin/python
# -*- coding: utf-8 -*-

"""
Proof of Concept Hadoop to shred files deleted from HDFS for audit compliance.
See https://github.com/Chaffleson/hdfs-shred
"""

import logging
import logging.handlers
from syslog_rfc5424_formatter import RFC5424Formatter
import re
import subprocess
import sys
import argparse
import pickle
from zlib import compress, decompress
from kazoo.client import KazooClient

from config import conf

# Set to True to enhance logging when working in a development environment
test_mode = True

log = logging.getLogger(__file__)
log.setLevel(logging.INFO)
handler = logging.handlers.SysLogHandler(address='/dev/log')
handler.setFormatter(RFC5424Formatter())
log.addHandler(handler)
if test_mode:
    con_handler = logging.StreamHandler()
    log.addHandler(con_handler)

### Begin Function definitions


def parse_args(args):
    parser = argparse.ArgumentParser(
        description="Proof of Concept Hadoop to shred files deleted from HDFS for audit compliance."
    )
    parser.add_argument('-v', '--version', action='version', version='%(prog)s {0}'.format(conf.VERSION))
    parser.add_argument('-m', '--mode', choices=('client', 'worker', 'shredder'),
                        help="Specify mode; 'client' submits a --filename to be deleted and shredded, "
                             "'worker' triggers this script to represent this Datanode when deleting a file from HDFS, "
                             "'shredder' triggers this script to check for and shred blocks on this Datanode")
    parser.add_argument('-f', '--filename', action="store", help="Specify a filename for the 'client' mode.")
    parser.add_argument('--debug', action="store_true", help='Increase logging verbosity.')
    log.debug("Parsing commandline args [{0}]".format(args))
    result = parser.parse_args(args)
    if result.debug:
        log.setLevel(logging.DEBUG)
    if result.mode is 'file' and result.filename is None:
        log.error("Argparse found a bad arg combination, posting info and quitting")
        parser.error("--mode 'file' requires a filename to register for shredding.")
    if result.mode is 'blocks' and result.filename:
        log.error("Argparse found a bad arg combination, posting info and quitting")
        parser.error("--mode 'blocks' cannot be used to register a new filename for shredding."
                     " Please try '--mode file' instead.")
    log.debug("Argparsing complete, returning args to main function")
    return result


def connect_zk(host):
    """create connection to ZooKeeper"""
    log.debug("Connecting to Zookeeper using host param [{0}]".format(host))
    zk = KazooClient(hosts=host)
    zk.start()
    if zk.state is 'CONNECTED':
        log.debug("Returning Zookeeper connection to main function")
        return zk
    else:
        raise "Could not connect to ZooKeeper with configuration string [{0}], resulting connection state was [{1}]"\
            .format(host, zk.state)


def run_shell_command(command):
    """Read output of shell command - line by line"""
    log.debug("Running Shell command [{0}]".format(command))
    # http://stackoverflow.com/a/13135985
    p = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT
        )
    log.debug("Returning iterable to calling function")
    return iter(p.stdout.readline, b'')


def check_hdfs_compat():
    """Checks if we can connect to HDFS and it's a tested version"""
    # TODO: Collect version number and pass back that or error
    # TODO: Ensure the HDFS:/.shred directory is available to work with
    hdfs_compat_iter = run_shell_command(['hdfs', 'version'])
    result = False
    firstline = hdfs_compat_iter.next()   # Firstline should be version number if it works
    for vers in conf.COMPAT:
        if vers in firstline:
            result = True
    return result


def check_hdfs_for_file(target):
    """
    Checks if file requested for shredding actually exists on HDFS.
    Returns True if file is Found.
    Returns Error details if it is not found.
    """
    # TODO: Return canonical path from LS command rather than trusting user input
    log.debug("Checking validity of HDFS File target [{0}]".format(target))
    file_check_isDir = subprocess.call(['hdfs', 'dfs', '-test', '-d', target])
    log.debug("File Check isDir returned [{0}]".format(file_check_isDir))
    if file_check_isDir is 0:   # Returns 0 on success
        raise ValueError("Target [{0}] is a directory.".format(target))
    file_check_isFile = subprocess.call(['hdfs', 'dfs', '-test', '-e', target])
    log.debug("File Check isFile returned [{0}]".format(file_check_isFile))
    if file_check_isFile is not 0:    # Returns 0 on success
        raise ValueError("Target [{0}]: File not found.".format(target))
    else:
        return True


def get_fsck_output(target):
    """Runs HDFS FSCK on the HDFS File to get block location information for Linux shredder"""
    # fsck_out_iter = run_shell_command(['cat', 'sample-data.txt'])
    fsck_out_iter = run_shell_command(["hdfs", "fsck", target, "-files", "-blocks", "-locations"])
    log.debug("Fsck_out type is [{0}]".format(type(fsck_out_iter)))
    return fsck_out_iter


def parse_blocks_from_fsck(raw_fsck):
    """Separate parser for FSCK output to make maintenance easier"""
    output = {}
    while True:
        try:
            current_line = raw_fsck.next()
            if current_line[0].isdigit():
                # TODO: Covert Block names to INTs and pack into job lots in ZK to reduce space
                output_split = current_line.split("[", 1)
                block_id = re.search(':(.+?) ', output_split[0]).group(1).rpartition("_")
                block_by_data_nodes = re.findall("DatanodeInfoWithStorage\[(.*?)\]", output_split[1])
                for block in block_by_data_nodes:
                    dn_ip = block.split(":", 1)
                    if dn_ip[0] not in output:
                        output[dn_ip[0]] = []
                    output[dn_ip[0]].append(block_id[0])
        except StopIteration:
            break
    log.debug("FSCK parser output [{0}]".format(output))
    return output


def write_blocks_to_zk(zk_conn, data):
    """Write block to be deleted to zookeeper"""
    log.debug("ZK Writer passed blocklists for [{0}] Datanodes to shred".format(len(data)))
    for datanode_ip in data:
        log.debug("Processing blocklist for Datanode [{0}]".format(datanode_ip))
        zk_path_dn = conf.ZOOKEEPER['PATH'] + datanode_ip
        zk_conn.ensure_path(zk_path_dn)
        zk_conn.create(
            path=zk_path_dn + '/blocklist',
            value=compress(pickle.dumps(data[datanode_ip]))
        )
        zk_conn.create(
            path=zk_path_dn + '/status',
            value='file_not_deleted_blocklist_written'
        )
    log.debug("List of DN Blocklists written to ZK: [{0}]".format(zk_conn.get_children(conf.ZOOKEEPER['PATH'])))
    # TODO: Test ZK nodes are all created as expected
    # TODO: Handle existing ZK nodes
    return True


def delete_file_from_hdfs(target):
    """Uses HDFS Client to delete the file from HDFS
    Returns a Bool result"""
    return True


def get_datanode_ip():
    """Returns the IP of this Datanode"""
    # TODO: Write this function to return more than a placeholder
    return "127.0.0.1"


def read_blocks_from_zk(zk_conn, dn_id):
    """
    Read blocks to be deleted from Zookeeper
    Requires active ZooKeeper connection and the datanode-ID as it's IP as a string
    """
    # TODO: Check dn_id is valid
    log.debug("Attempting to read blocklist for Datanode [{0}]".format(dn_id))
    zk_path_dn = conf.ZOOKEEPER['PATH'] + dn_id
    dn_status = zk_conn.get(zk_path_dn + '/status')
    if dn_status[0] is 'file_not_deleted_blocklist_written':
        dn_node = zk_conn.get(zk_path_dn + '/blocklist')
        blocklist = pickle.loads(decompress(dn_node[0]))
        return blocklist
    else:
        raise ValueError("Blocklist Status for this DN is not as expected at [{0}]".format(zk_path_dn + '/status'))


def generate_shred_task_list(block_list):
    """Generate list of tasks of blocks to shred for this host"""
    # TODO: Write this function to return more than a placeholder
    output = {}
    return output


def shred_blocks(blocks):
    """Reliable shred process to ensure blocks are truly gone baby gone"""
    # TODO: Write this function to return more than a placeholder
    # TODO: Keep tracked of deleted block / files
    pass


def check_job_status(job_id):
    """Checks for job status in ZK and returns meaningful codes"""
    # TODO: return 'JobNotFound' if file is not listed in ZK
    pass


def write_job_zk(job_id):
    """Writes the filepath to ZK as a new delete/shred job"""
    pass


def ingest_file_to_shred(target):
    """Moves file from initial location to shred worker folder on HDFS and generates job management files"""
    pass
    # Create directory named with a guid, create status file of guid name in shred root with 'stage1prepare' in it
    # Create subdirs for data and datanode
    # update status to 'stage1ingesttargets'
    # Move all files to the data directory
    # update status to 'stage1complete'
    # return status and job guid

### End Function definitions


def main():
    ### Program setup
    log.info("shred.py called with args [{0}]").format(sys.argv[1:])
    # Get invoke parameters
    log.debug("Parsing args using Argparse module.")
    args = parse_args(sys.argv[1:])                                             # Test Written
    # Checking the config was pulled in
    log.debug("Checking for config parameters.")
    if not conf.VERSION:
        raise "Config from config.py not found, please check configuration file is available and try again."
    # Test necessary connections
    log.debug("Checking if we can find the HDFS client and HDFS instance to connect to.")
    if not check_hdfs_compat:                                                   # Test Written
        raise "Could not find HDFS, please check the HDFS client is installed and HDFS is available and try again."
    # Test for Zookeeper connectivity
    log.debug("Looking for ZooKeeper")
    ### End Program Setup

    if args.mode is 'client':
        if args.mode is 'client':
            log.debug("Detected that we're running in 'client' Mode")
            log.debug("Checking if file exists in HDFS")
            file_exists = check_hdfs_for_file(args.file_to_shred)
            if file_exists is not True:
                raise "Submitted File not found on HDFS: [{0}]".format(args.file_to_shred)
            else:
                # By using the client to move the file to the shred location we validate that the user has permissions
                # to call for the delete and shred
                ingest_result = ingest_file_to_shred(args.file_to_shred)
                if ingest_result is not True:
                    raise "Could not take control of submitted file: [{0}]".format(args.file_to_shred)
                else:
                    # TODO: Return a success flag and shred path for logging / display to user
                    log.debug("Job created, exiting with success")
                    print("Successfully created delete and shred job for file [{0}]".format(args.file_to_shred))
                    exit(0)
    elif args.mode is 'worker':
        pass
        # wake from sleep mode
        # check if there are new files in HDFS:/.shred indicating new jobs to be done
        # if no new jobs, sleep
        # else, check files for status
        # if status is stage1complete, connect to ZK
        zk_host = conf.ZOOKEEPER['HOST'] + ':' + str(conf.ZOOKEEPER['PORT'])
        zk = connect_zk(zk_host)                                                # Test Written
        # if no guid node, attempt to kazoo lease new guid node for 2x sleep period minutes
        # http://kazoo.readthedocs.io/en/latest/api/recipe/lease.html
        # if not get lease, pass, else:
        # update job status to stage2prepareblocklist
        # parse fsck for blocklist, write to hdfs job subdir for other workers to read 
        # update job status to stage2copyblocks
        # release lease
        #
        # Foreach job in subdirs
        # if status is stage2copyblocks
        # parse blocklist for job
        # if blocks for this DN
        # update DN status to Stage2running
        # create tasklist file under DN subdir in job
        # foreach blockfile in job:
        # update tasklist file to finding
        # find blockfile on ext4
        # update tasklist file to copying
        # create hardlink to .shred dir on same partition
        # update tasklist file to copied
        # When all blocks finished, update DN status to Stage2complete
        #
        # if all blocks copied, attempt lease of guid for 2x sleep period minutes, else sleep for 1x period minutes
        # if lease:
        # Update DN status to Stage2leaderactive
        # periodically check status file of each DN against DNs in blocklist
        # if DN not working within 2x sleep period minutes, alert
        # if DN all working but not finished, update Dn status to Stage2leaderwait, short sleep
        # if all DN report finished cp, update job status stage2readyfordelete
        # run hdfs delete files -skiptrash, update status to stage2filesdeleted
        # update central status as stage2complete, update Dn status as Stage2complete
        # release lease, shutdown
    elif args.mode is 'shredder':
        pass
        # wake on schedule
        # Foreach job in subdir:
        # if Stage2Complete
        # Get DN tasklist for job
        # Set DN status to Shredding for job
        # Foreach blockfile:
        # set status to shredding in tasklist
        # run shred
        # set status to shredded in tasklist
        # when job complete, set DN status to Stage3complete
    else:
        raise "Bad operating mode [{0}] detected. Please consult program help and try again.".format(args.mode)


if __name__ == "__main__":
    main()

