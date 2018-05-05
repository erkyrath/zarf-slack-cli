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
    pass

def try_shutdown():
    global shutdown_thread
    print('<KeyboardInterrupt>')
    shutdown_thread = True
    if not thread:
        return True

def thread_main():
    global thread
    while not shutdown_thread:
        print('Tick.')
        time.sleep(1)
    print('Disconnected.')
    thread = None


thread = threading.Thread(target=thread_main, name='slack-comm')
thread.start()

application = prompt_toolkit.shortcuts.create_prompt_application('>')

while thread:
    try:
        #input = prompt_toolkit.prompt(
        #    '>',
        #    eventloop=prompt_toolkit.shortcuts.create_eventloop(inputhook=check_background),
        #    patch_stdout=True)
        eventloop = prompt_toolkit.shortcuts.create_eventloop(inputhook=check_background)
        input = prompt_toolkit.shortcuts.run_application(
            application,
            patch_stdout=True,
            refresh_interval=0,
            eventloop=eventloop)
        print('### input:', input)
    except KeyboardInterrupt:
        res = try_shutdown()
        if res:
            break
    except EOFError:
        res = try_shutdown()
        if res:
            break

    
