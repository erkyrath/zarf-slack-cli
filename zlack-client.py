#!/usr/bin/env python3

# http://python-prompt-toolkit.readthedocs.io/en/master/pages/building_prompts.html

import sys
import os
import time
import optparse
import asyncio
import threading
import prompt_toolkit
from slackclient import SlackClient

application = None
thread = None
shutdown_thread = False

lock = threading.Lock()
input_list = []
output_list = []

async def input_loop():
    global shutdown_thread
    while thread:
        try:
            input = await prompt_toolkit.prompt_async('>', patch_stdout=True)
            add_input(input)
        except KeyboardInterrupt:
            print('<KeyboardInterrupt>')
            shutdown_thread = True
        except EOFError:
            print('<EOFError>')
            shutdown_thread = True

def check_for_outputs(evloop):
    ls = fetch_outputs();
    for ln in ls:
        print(ln)
    if not ls:
        print('(no output)')
    if thread:
        evloop.call_later(1.0, check_for_outputs, evloop)
        
def thread_main():
    global thread
    while not shutdown_thread:
        ls = fetch_inputs()
        for ln in ls:
            add_output('Processed: ' + ln)
        time.sleep(1)
    add_output('Disconnected.')
    thread = None

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

evloop = asyncio.get_event_loop()

evloop.call_soon(check_for_outputs, evloop)

evloop.run_until_complete(input_loop())
evloop.close()
