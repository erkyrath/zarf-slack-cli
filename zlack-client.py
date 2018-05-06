#!/usr/bin/env python3

# http://python-prompt-toolkit.readthedocs.io/en/master/pages/building_prompts.html

import sys
import os
import time
import optparse
import threading
import prompt_toolkit
from slackclient import SlackClient

application = None
thread = None
shutdown_thread = False

def check_background(ctx):
    ls = fetch_outputs()
    for ln in ls:
        print(ln)

def try_shutdown():
    global shutdown_thread
    print('<KeyboardInterrupt>')
    shutdown_thread = True
    if not thread:
        return True

def thread_main():
    global thread
    while not shutdown_thread:
        ls = fetch_inputs()
        for ln in ls:
            add_output('Processed: ' + ln)
        time.sleep(1)
    print('Disconnected.')
    thread = None

lock = threading.Lock()
input_list = []
output_list = []

def add_input(val):
    with lock:
        input_list.append(val)
        
def fetch_inputs():
    res = []
    with lock:
        if input_list:
            res.extend(input_list)
            input_list[:] = []
    return res

def add_output(val):
    with lock:
        output_list.append(val)
        
def fetch_outputs():
    res = []
    with lock:
        if output_list:
            res.extend(output_list)
            output_list[:] = []
    return res

thread = threading.Thread(target=thread_main, name='slack-comm')
thread.start()

application = prompt_toolkit.shortcuts.create_prompt_application('>')

while thread:
    try:
        input = prompt_toolkit.shortcuts.run_application(
            application,
            patch_stdout=True,
            refresh_interval=0,
            eventloop=prompt_toolkit.shortcuts.create_eventloop(inputhook=check_background))
        input = input.rstrip()
        if input:
            add_input(input)
    except KeyboardInterrupt:
        res = try_shutdown()
        if res:
            break
    except EOFError:
        res = try_shutdown()
        if res:
            break

    
