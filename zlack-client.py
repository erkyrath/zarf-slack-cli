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

thread = None

class SlackThread(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self, name='slack-thread')
        self.input_list = []
        self.output_list = []
        self.want_shutdown = False
        self.lock = threading.Lock()
        
    def run(self):
        while not self.want_shutdown:
            ls = self.fetch_inputs()
            for ln in ls:
                self.add_output('Processed: ' + ln)
            time.sleep(1)
        self.add_output('Disconnected.')

    def set_shutdown(self):
        with self.lock:
            self.want_shutdown = True

    def add_input(self, val):
        with self.lock:
            self.input_list.append(val)
            
    def fetch_inputs(self):
        res = []
        with self.lock:
            if self.input_list:
                res.extend(self.input_list)
                self.input_list[:] = []
        return res
    
    def add_output(self, val):
        with self.lock:
            self.output_list.append(val)
            
    def fetch_outputs(self):
        res = []
        with self.lock:
            if self.output_list:
                res.extend(self.output_list)
                self.output_list[:] = []
        return res

async def input_loop():
    while thread.is_alive():
        try:
            input = await prompt_toolkit.prompt_async('>', patch_stdout=True)
            thread.add_input(input)
        except KeyboardInterrupt:
            print('<KeyboardInterrupt>')
            thread.set_shutdown()
        except EOFError:
            print('<EOFError>')
            thread.set_shutdown()

def check_for_outputs(evloop):
    ls = thread.fetch_outputs();
    for ln in ls:
        print(ln)
    if not ls:
        print('(no output)')
    if thread.is_alive():
        evloop.call_later(1.0, check_for_outputs, evloop)
        
thread = SlackThread()
thread.start()

evloop = asyncio.get_event_loop()

evloop.call_soon(check_for_outputs, evloop)

evloop.run_until_complete(input_loop())
evloop.close()
