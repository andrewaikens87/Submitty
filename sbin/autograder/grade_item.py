import configparser
import json
import os
import tempfile
import shutil
import subprocess
import stat
import time
import dateutil
import dateutil.parser
import urllib.parse
import string
import random
import socket
import zipfile
import sys

from submitty_utils import dateutils, glob
from . import grade_items_logging, grade_item_main_runner, write_grade_history, CONFIG_PATH

with open(os.path.join(CONFIG_PATH, 'submitty.json')) as open_file:
    OPEN_JSON = json.load(open_file)
SUBMITTY_INSTALL_DIR = OPEN_JSON['submitty_install_dir']
SUBMITTY_DATA_DIR = OPEN_JSON['submitty_data_dir']

with open(os.path.join(CONFIG_PATH, 'submitty_users.json')) as open_file:
    OPEN_JSON = json.load(open_file)
DAEMON_UID = OPEN_JSON['daemon_uid']


# NOTE: DOCKER SUPPORT PRELIMINARY -- NEEDS MORE SECURITY BEFORE DEPLOYED ON LIVE SERVER
USE_DOCKER = False

def add_permissions(item,perms):
    if os.getuid() == os.stat(item).st_uid:
        os.chmod(item,os.stat(item).st_mode | perms)
    # else, can't change permissions on this file/directory!


def touch(my_file):
    with open(my_file,'a') as tmp:
        os.utime(my_file, None)

                
def add_permissions_recursive(top_dir,root_perms,dir_perms,file_perms):
    for root, dirs, files in os.walk(top_dir):
        add_permissions(root,root_perms)
        for d in dirs:
            add_permissions(os.path.join(root, d),dir_perms)
        for f in files:
            add_permissions(os.path.join(root, f),file_perms)

# copy the files & directories from source to target
# it will create directories as needed
# it's ok if the target directory or subdirectories already exist
# it will overwrite files with the same name if they exist
def copy_contents_into(job_id,source,target,tmp_logs):
    if not os.path.isdir(target):
        grade_items_logging.log_message(job_id,message="ERROR: the target directory does not exist " + target)
        raise RuntimeError("ERROR: the target directory does not exist '", target, "'")
    if os.path.isdir(source):
        for item in os.listdir(source):
            if os.path.isdir(os.path.join(source,item)):
                if os.path.isdir(os.path.join(target,item)):
                    # recurse
                    copy_contents_into(job_id,os.path.join(source,item),os.path.join(target,item),tmp_logs)
                elif os.path.isfile(os.path.join(target,item)):
                    grade_items_logging.log_message(job_id,message="ERROR: the target subpath is a file not a directory '" + os.path.join(target,item) + "'")
                    raise RuntimeError("ERROR: the target subpath is a file not a directory '", os.path.join(target,item), "'")
                else:
                    # copy entire subtree
                    shutil.copytree(os.path.join(source,item),os.path.join(target,item))
            else:
                if os.path.exists(os.path.join(target,item)):
                    with open(os.path.join(tmp_logs,"overall.txt"),'a') as f:
                        print ("\nWARNING: REMOVING DESTINATION FILE" , os.path.join(target,item),
                               " THEN OVERWRITING: ", os.path.join(source,item), "\n", file=f)
                    os.remove(os.path.join(target,item))
                try:
                    shutil.copy(os.path.join(source,item),target)
                except:
                    raise RuntimeError("ERROR COPYING FILE: " +  os.path.join(source,item) + " -> " + os.path.join(target,item))


# copy files that match one of the patterns from the source directory
# to the target directory.  
def pattern_copy(what,patterns,source,target,tmp_logs):
    with open(os.path.join(tmp_logs,"overall.txt"),'a') as f:
        print (what," pattern copy ", patterns, " from ", source, " -> ", target, file=f)
        for pattern in patterns:
            for my_file in glob.glob(os.path.join(source,pattern),recursive=True):
                if (os.path.isfile(my_file)):
                    # grab the matched name
                    relpath = os.path.relpath(my_file,source)
                    # make the necessary directories leading to the file
                    os.makedirs(os.path.join(target,os.path.dirname(relpath)),exist_ok=True)
                    # copy the file
                    shutil.copy(my_file,os.path.join(target,relpath))
                    print ("    COPY ",my_file,
                           " -> ",os.path.join(target,relpath), file=f)
                else:
                    print ("skip this directory (will recurse into it later)", my_file, file=f)

# give permissions to all created files to the DAEMON_USER
def untrusted_grant_rwx_access(which_untrusted,my_dir):
    subprocess.call([os.path.join(SUBMITTY_INSTALL_DIR, "sbin", "untrusted_execute"),
                     which_untrusted,
                     "/usr/bin/find",
                     my_dir,
                     "-user",
                     which_untrusted,
                     "-exec",
                     "/bin/chmod",
                     "o+rwx",
                     "{}",
                     ";"])


def zip_my_directory(path,zipfilename):
    zipf = zipfile.ZipFile(zipfilename,'w',zipfile.ZIP_DEFLATED)
    for root,dirs,files in os.walk(path):
        for my_file in files:
            relpath = root[len(path)+1:]
            zipf.write(os.path.join(root,my_file),os.path.join(relpath,my_file))
    zipf.close()


def unzip_this_file(zipfilename,path):
    if not os.path.exists(zipfilename):
        raise RuntimeError("ERROR: zip file does not exist '", zipfilename, "'")
    zip_ref = zipfile.ZipFile(zipfilename,'r')
    zip_ref.extractall(path)
    zip_ref.close()



# ==================================================================================
# ==================================================================================
# ==================================================================================
# ==================================================================================

def grade_from_zip(my_autograding_zip_file,my_submission_zip_file,which_untrusted):
    os.chdir(SUBMITTY_DATA_DIR)
    tmp = os.path.join("/var/local/submitty/autograding_tmp/",which_untrusted,"tmp")

    # clean up old usage of this directory
    shutil.rmtree(tmp,ignore_errors=True)
    os.mkdir(tmp)

    which_machine=socket.gethostname()

    # unzip autograding and submission folders
    tmp_autograding = os.path.join(tmp,"TMP_AUTOGRADING")
    tmp_submission = os.path.join(tmp,"TMP_SUBMISSION")
    unzip_this_file(my_autograding_zip_file,tmp_autograding)
    unzip_this_file(my_submission_zip_file,tmp_submission)
    os.remove(my_autograding_zip_file)
    os.remove(my_submission_zip_file)

    tmp_logs = os.path.join(tmp,"TMP_SUBMISSION","tmp_logs")

    queue_file = os.path.join(tmp_submission,"queue_file.json")
    with open(queue_file, 'r') as infile:
        queue_obj = json.load(infile)

    queue_time_longstring = queue_obj["queue_time"]
    waittime = queue_obj["waittime"]
    is_batch_job = queue_obj["regrade"]
    job_id = queue_obj["job_id"]
    is_batch_job_string = "BATCH" if is_batch_job else "INTERACTIVE"

    partial_path = os.path.join(queue_obj["gradeable"],queue_obj["who"],str(queue_obj["version"]))
    item_name = os.path.join(queue_obj["semester"],queue_obj["course"],"submissions",partial_path)

    grade_items_logging.log_message(job_id,is_batch_job,which_untrusted,item_name,"wait:",waittime,"")

    with open(os.path.join(tmp_submission,".grading_began"), 'r') as f:
        grading_began_longstring = f.read()
    grading_began = dateutils.read_submitty_date(grading_began_longstring)

    # --------------------------------------------------------------------
    # START DOCKER

    # WIP: This option file facilitated testing...
    #USE_DOCKER = os.path.isfile("/tmp/use_docker")
    #use_docker_string="grading begins, using DOCKER" if USE_DOCKER else "grading begins (not using docker)"
    #grade_items_logging.log_message(job_id,is_batch_job,which_untrusted,submission_path,message=use_docker_string)
    
    container = None
    if USE_DOCKER:
        container = subprocess.check_output(['docker', 'run', '-t', '-d',
                                             '-v', tmp + ':' + tmp,
                                             'ubuntu:custom']).decode('utf8').strip()
        dockerlaunch_done=dateutils.get_current_time()
        dockerlaunch_time = (dockerlaunch_done-grading_began).total_seconds()
        grade_items_logging.log_message(job_id,is_batch_job,which_untrusted,item_name,"dcct:",dockerlaunch_time,"docker container created")

    # --------------------------------------------------------------------
    # COMPILE THE SUBMITTED CODE

    with open(os.path.join(tmp_logs, "overall.txt"), 'a') as f:
        print("====================================\nCOMPILATION STARTS", file=f)
    
    # copy submitted files to the tmp compilation directory
    tmp_compilation = os.path.join(tmp,"TMP_COMPILATION")
    os.mkdir(tmp_compilation)
    os.chdir(tmp_compilation)

    submission_path = os.path.join(tmp_submission,"submission")
    checkout_path = os.path.join(tmp_submission,"checkout")

    provided_code_path = os.path.join(tmp_autograding,"provided_code")
    test_input_path = os.path.join(tmp_autograding,"test_input")
    test_output_path = os.path.join(tmp_autograding,"test_output")
    custom_validation_code_path = os.path.join(tmp_autograding,"custom_validation_code")
    bin_path = os.path.join(tmp_autograding,"bin")
    form_json_config = os.path.join(tmp_autograding,"form.json")
    complete_config = os.path.join(tmp_autograding,"complete_config.json")


    with open(form_json_config, 'r') as infile:
        gradeable_config_obj = json.load(infile)
    gradeable_deadline_string = gradeable_config_obj["date_due"]
    
    with open(complete_config, 'r') as infile:
        complete_config_obj = json.load(infile)
    patterns_submission_to_compilation = complete_config_obj["autograding"]["submission_to_compilation"]
    pattern_copy("submission_to_compilation",patterns_submission_to_compilation,submission_path,tmp_compilation,tmp_logs)

    is_vcs = gradeable_config_obj["upload_type"]=="repository"
    checkout_subdirectory = complete_config_obj["autograding"].get("use_checkout_subdirectory","")
    checkout_subdir_path = os.path.join(checkout_path,checkout_subdirectory)

    if is_vcs:
        pattern_copy("checkout_to_compilation",patterns_submission_to_compilation,checkout_subdir_path,tmp_compilation,tmp_logs)
    
    # copy any instructor provided code files to tmp compilation directory
    copy_contents_into(job_id,provided_code_path,tmp_compilation,tmp_logs)

    subprocess.call(['ls', '-lR', '.'], stdout=open(tmp_logs + "/overall.txt", 'a'))

    # copy compile.out to the current directory
    shutil.copy (os.path.join(bin_path,"compile.out"),os.path.join(tmp_compilation,"my_compile.out"))

    # give the untrusted user read/write/execute permissions on the tmp directory & files
    add_permissions_recursive(tmp_compilation,
                              stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP,
                              stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP,
                              stat.S_IRGRP | stat.S_IWGRP | stat.S_IXGRP)

    # add_permissions(tmp,stat.S_IROTH | stat.S_IXOTH) #stat.S_ISGID
    add_permissions(tmp_logs,stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    

    # grab the submission time
    with open (os.path.join(submission_path,".submit.timestamp"), 'r') as submission_time_file:
        submission_string = submission_time_file.read().rstrip()

    with open(os.path.join(tmp_logs,"compilation_log.txt"), 'w') as logfile:
        if USE_DOCKER:
            compile_success = subprocess.call(['docker', 'exec', '-w', tmp_compilation, container,
                                               os.path.join(tmp_compilation, 'my_compile.out'), queue_obj['gradeable'],
                                               queue_obj['who'], str(queue_obj['version']), submission_string], stdout=logfile)
        else:
            compile_success = subprocess.call([os.path.join(SUBMITTY_INSTALL_DIR, "sbin", "untrusted_execute"),
                                               which_untrusted,
                                               os.path.join(tmp_compilation,"my_compile.out"),
                                               queue_obj["gradeable"],
                                               queue_obj["who"],
                                               str(queue_obj["version"]),
                                               submission_string],
                                              stdout=logfile)

    if compile_success == 0:
        print (which_machine,which_untrusted,"COMPILATION OK")
    else:
        print (which_machine,which_untrusted,"COMPILATION FAILURE")
        grade_items_logging.log_message(job_id,is_batch_job,which_untrusted,item_name,message="COMPILATION FAILURE")

    untrusted_grant_rwx_access(which_untrusted,tmp_compilation)
        
    # remove the compilation program
    os.remove(os.path.join(tmp_compilation,"my_compile.out"))

    # return to the main tmp directory
    os.chdir(tmp)


    # --------------------------------------------------------------------
    # make the runner directory

    with open(os.path.join(tmp_logs,"overall.txt"),'a') as f:
        print ("====================================\nRUNNER STARTS", file=f)
        
    tmp_work = os.path.join(tmp,"TMP_WORK")
    tmp_work_test_input = os.path.join(tmp_work, "test_input")
    tmp_work_subission = os.path.join(tmp_work, "submission")
    tmp_work_checkout = os.path.join(tmp_work, "checkout")
    
    os.mkdir(tmp_work)
    
    os.mkdir(tmp_work_test_input)
    os.mkdir(tmp_work_subission)
    os.mkdir(tmp_work_checkout)
    
    os.chdir(tmp_work)

    # move all executable files from the compilation directory to the main tmp directory
    # Note: Must preserve the directory structure of compiled files (esp for Java)

    patterns_submission_to_runner = complete_config_obj["autograding"]["submission_to_runner"]
    pattern_copy("submission_to_runner",patterns_submission_to_runner,submission_path,tmp_work_subission,tmp_logs)
    if is_vcs:
        pattern_copy("checkout_to_runner",patterns_submission_to_runner,checkout_subdir_path,tmp_work_checkout,tmp_logs)

    patterns_compilation_to_runner = complete_config_obj["autograding"]["compilation_to_runner"]
    #TODO what does this do?
    pattern_copy("compilation_to_runner",patterns_compilation_to_runner,tmp_compilation,tmp_work,tmp_logs)
        
    # copy input files to tmp_work directory
    copy_contents_into(job_id,test_input_path,tmp_work_test_input,tmp_logs)

    subprocess.call(['ls', '-lR', '.'], stdout=open(tmp_logs + "/overall.txt", 'a'))

    # copy runner.out to the current directory
    shutil.copy (os.path.join(bin_path,"run.out"),os.path.join(tmp_work,"my_runner.out"))

    #set the appropriate permissions for the newly created directories 
    #TODO replaces commented out code below
    add_permissions(os.path.join(tmp_work,"my_runner.out"), stat.S_IXUSR | stat.S_IXGRP |stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH)
    add_permissions(os.path.join(tmp_work,"submission"), stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH)
    add_permissions(os.path.join(tmp_work,"checkout"), stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH)

    #TODO this is how permissions used to be set. It was removed because of the way it interacts with the sticky bit.
    ## give the untrusted user read/write/execute permissions on the tmp directory & files
    # os.system('ls -al {0}'.format(tmp_work))
    # add_permissions_recursive(tmp_work,
    #                           stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH,
    #                           stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH,
    #                           stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH)


    ##################################################################################################
    #call grade_item_main_runner.py
    runner_success = grade_item_main_runner.executeTestcases(complete_config_obj, tmp_logs, tmp_work, queue_obj, submission_string, 
                                                                                    item_name, USE_DOCKER, container, which_untrusted)
    ##################################################################################################
    if runner_success == 0:
        print (which_machine,which_untrusted, "RUNNER OK")
    else:
        print (which_machine,which_untrusted, "RUNNER FAILURE")
        grade_items_logging.log_message(job_id, is_batch_job, which_untrusted, item_name, message="RUNNER FAILURE")

    untrusted_grant_rwx_access(which_untrusted, tmp_work)
    untrusted_grant_rwx_access(which_untrusted, tmp_compilation)

    # --------------------------------------------------------------------
    # RUN VALIDATOR

    with open(os.path.join(tmp_logs,"overall.txt"),'a') as f:
        print ("====================================\nVALIDATION STARTS", file=f)

    # copy results files from compilation...
    patterns_submission_to_validation = complete_config_obj["autograding"]["submission_to_validation"]
    pattern_copy("submission_to_validation",patterns_submission_to_validation,submission_path,tmp_work,tmp_logs)
    if is_vcs:
        pattern_copy("checkout_to_validation",patterns_submission_to_validation,checkout_subdir_path,tmp_work,tmp_logs)
    patterns_compilation_to_validation = complete_config_obj["autograding"]["compilation_to_validation"]
    pattern_copy("compilation_to_validation",patterns_compilation_to_validation,tmp_compilation,tmp_work,tmp_logs)

    # remove the compilation directory
    shutil.rmtree(tmp_compilation)

    # copy output files to tmp_work directory
    copy_contents_into(job_id,test_output_path,tmp_work,tmp_logs)

    # copy any instructor custom validation code into the tmp work directory
    copy_contents_into(job_id,custom_validation_code_path,tmp_work,tmp_logs)

    subprocess.call(['ls', '-lR', '.'], stdout=open(tmp_logs + "/overall.txt", 'a'))

    # copy validator.out to the current directory
    shutil.copy (os.path.join(bin_path,"validate.out"),os.path.join(tmp_work,"my_validator.out"))

    # give the untrusted user read/write/execute permissions on the tmp directory & files
    add_permissions_recursive(tmp_work,
                              stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH,
                              stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH,
                              stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH)

    add_permissions(os.path.join(tmp_work,"my_validator.out"), stat.S_IXUSR | stat.S_IXGRP |stat.S_IROTH | stat.S_IWOTH | stat.S_IXOTH)

    #todo remove prints.
    print("VALIDATING")
    print('this is the command that was to be used: \n{0} {1} {2} {3} {4} {5} {6}'.format( 
                                                 os.path.join(SUBMITTY_INSTALL_DIR,"sbin","untrusted_execute"),
                                                 which_untrusted,
                                                 os.path.join(tmp_work,"my_validator.out"),
                                                 queue_obj["gradeable"],
                                                 queue_obj["who"],
                                                 str(queue_obj["version"]),
                                                 submission_string))
    # validator the validator.out as the untrusted user
    with open(os.path.join(tmp_logs,"validator_log.txt"), 'w') as logfile:
        if USE_DOCKER:
            validator_success = subprocess.call(['docker', 'exec', '-w', tmp_work, container,
                                                 os.path.join(tmp_work, 'my_validator.out'), queue_obj['gradeable'],
                                                 queue_obj['who'], str(queue_obj['version']), submission_string], stdout=logfile)
        else:
            validator_success = subprocess.call([os.path.join(SUBMITTY_INSTALL_DIR,"sbin","untrusted_execute"),
                                                 which_untrusted,
                                                 os.path.join(tmp_work,"my_validator.out"),
                                                 queue_obj["gradeable"],
                                                 queue_obj["who"],
                                                 str(queue_obj["version"]),
                                                 submission_string],
                                                stdout=logfile)

    if validator_success == 0:
        print (which_machine,which_untrusted,"VALIDATOR OK")
    else:
        print (which_machine,which_untrusted,"VALIDATOR FAILURE")
        grade_items_logging.log_message(job_id,is_batch_job,which_untrusted,item_name,message="VALIDATION FAILURE")

    untrusted_grant_rwx_access(which_untrusted,tmp_work)

    # grab the result of autograding
    grade_result = ""
    with open(os.path.join(tmp_work,"grade.txt")) as f:
        lines = f.readlines()
        for line in lines:
            line = line.rstrip('\n')
            if line.startswith("Automatic grading total:"):
                grade_result = line


    # --------------------------------------------------------------------
    # MAKE RESULTS DIRECTORY & COPY ALL THE FILES THERE
    tmp_results = os.path.join(tmp,"TMP_RESULTS")

    with open(os.path.join(tmp_logs,"overall.txt"),'a') as f:
        print ("====================================\nARCHIVING STARTS", file=f)

    subprocess.call(['ls', '-lR', '.'], stdout=open(tmp_logs + "/overall.txt", 'a'))

    os.makedirs(os.path.join(tmp_results,"details"))

    patterns_work_to_details = complete_config_obj["autograding"]["work_to_details"]
    pattern_copy("work_to_details",patterns_work_to_details,tmp_work,os.path.join(tmp_results,"details"),tmp_logs)

    history_file_tmp = os.path.join(tmp_submission,"history.json")
    history_file = os.path.join(tmp_results,"history.json")
    if os.path.isfile(history_file_tmp):
        shutil.move(history_file_tmp,history_file)
        # fix permissions
        ta_group_id = os.stat(tmp_results).st_gid
        os.chown(history_file,int(DAEMON_UID),ta_group_id)
        add_permissions(history_file,stat.S_IRGRP)
    grading_finished = dateutils.get_current_time()

    shutil.copy(os.path.join(tmp_work,"grade.txt"),tmp_results)

    # -------------------------------------------------------------
    # create/append to the results history

    # grab the submission time
    with open (os.path.join(submission_path,".submit.timestamp")) as submission_time_file:
        submission_string = submission_time_file.read().rstrip()
    submission_datetime = dateutils.read_submitty_date(submission_string)

    gradeable_deadline_datetime = dateutils.read_submitty_date(gradeable_deadline_string)
    gradeable_deadline_longstring = dateutils.write_submitty_date(gradeable_deadline_datetime)
    submission_longstring = dateutils.write_submitty_date(submission_datetime)
    
    seconds_late = int((submission_datetime-gradeable_deadline_datetime).total_seconds())
    # note: negative = not late

    grading_finished_longstring = dateutils.write_submitty_date(grading_finished)

    gradingtime = (grading_finished-grading_began).total_seconds()

    with open(os.path.join(tmp_submission,"queue_file.json"), 'r') as infile:
        queue_obj = json.load(infile)
    queue_obj["gradingtime"]=gradingtime
    queue_obj["grade_result"]=grade_result
    queue_obj["which_untrusted"]=which_untrusted

    with open(os.path.join(tmp_results,"queue_file.json"),'w') as outfile:
        json.dump(queue_obj,outfile,sort_keys=True,indent=4,separators=(',', ': '))

    with open(os.path.join(tmp_work,"results.json"), 'r') as read_file:
        results_obj = json.load(read_file)
    if 'revision' in queue_obj.keys():
        results_obj['revision'] = queue_obj['revision']
    with open(os.path.join(tmp_results,"results.json"), 'w') as outfile:
        json.dump(results_obj,outfile,sort_keys=True,indent=4,separators=(',', ': '))

    write_grade_history.just_write_grade_history(history_file,
                                                 gradeable_deadline_longstring,
                                                 submission_longstring,
                                                 seconds_late,
                                                 queue_time_longstring,
                                                 is_batch_job_string,
                                                 grading_began_longstring,
                                                 int(waittime),
                                                 grading_finished_longstring,
                                                 int(gradingtime),
                                                 grade_result)

    os.chdir(SUBMITTY_DATA_DIR)

    if USE_DOCKER:
        with open(os.path.join(tmp_logs,"overall_log.txt"), 'w') as logfile:
            chmod_success = subprocess.call(['docker', 'exec', '-w', tmp_work, container,
                                             'chmod', '-R', 'o+rwx', '.'], stdout=logfile)

    with open(os.path.join(tmp_logs,"overall.txt"),'a') as f:
        f.write("FINISHED GRADING!\n")

    # save the logs!
    shutil.copytree(tmp_logs,os.path.join(tmp_results,"logs"))

    # zip up results folder
    filehandle, my_results_zip_file=tempfile.mkstemp()
    zip_my_directory(tmp_results,my_results_zip_file)
    os.close(filehandle)
    shutil.rmtree(tmp_autograding)
    shutil.rmtree(tmp_submission)
    shutil.rmtree(tmp_results)
    shutil.rmtree(tmp_work)
    shutil.rmtree(tmp)

    # WIP: extra logging for testing
    #grade_items_logging.log_message(job_id,is_batch_job,which_untrusted,submission_path,message="done grading")

    # --------------------------------------------------------------------
    # CLEAN UP DOCKER
    if USE_DOCKER:
        subprocess.call(['docker', 'rm', '-f', container])
        dockerdestroy_done=dateutils.get_current_time()
        dockerdestroy_time = (dockerdestroy_done-grading_finished).total_seconds()
        grade_items_logging.log_message(job_id,is_batch_job,which_untrusted,item_name,"ddt:",dockerdestroy_time,"docker container destroyed")
        
    grade_items_logging.log_message(job_id,is_batch_job,which_untrusted,item_name,"grade:",gradingtime,grade_result)

    return my_results_zip_file


# ==================================================================================
# ==================================================================================

if __name__ == "__main__":
    raise SystemExit('ERROR: Do not call this script directly')

