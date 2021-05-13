# -*- coding: utf-8 -*-

import json
import jsonschema
import jupyter_client
import os
import pytest
import time
import zmq


# def pytest_addoption(parser):
#     group = parser.getgroup('jupyter_kernel')
#     group.addoption(
#         '--foo',
#         action='store',
#         dest='dest_foo',
#         default='2021',
#         help='Set the value for the fixture "bar".'
#     )

#     parser.addini('HELLO', 'Dummy pytest.ini setting')


class Kernel(object):
    def __init__(self, kernel_name):
        self.kernel = jupyter_client.KernelManager(kernel_name=kernel_name)
        self.pending = {}
        with open(os.path.join(os.path.dirname(__file__), 'message-schema.json')) as f:
            message_schema = json.load(f)
            jsonschema.Draft7Validator.check_schema(message_schema)
            self.message_validator = jsonschema.Draft7Validator(message_schema)

    def start(self):
        self.kernel.start_kernel()
        self.client = self.kernel.client()
        self.client.start_channels()
        self.client.wait_for_ready()

    def validate_message(self, msg, source):
        msg_type = msg['header']['msg_type']
        self.message_validator.validate(msg)
        #if msg["parent_header"] is not None and "msg_id" in msg["parent_header"]:
        #    assert msg["parent_header"]["msg_id"] in self.pending, "Unknown parent message id."

    def read_replies(self, timeout = None, stdin_hook = None,
                     keep_status = False):
        messages = {}
        replies = {}
        idle = {}

        if timeout is not None:
            deadline = time.monotonic() + timeout
        else:
            timeout_ms = None

        poller = zmq.Poller()

        iopub_socket = self.client.iopub_channel.socket
        poller.register(iopub_socket, zmq.POLLIN)

        control_socket = self.client.control_channel.socket
        poller.register(control_socket, zmq.POLLIN)

        shell_socket = self.client.shell_channel.socket
        poller.register(shell_socket, zmq.POLLIN)

        stdin_socket = self.client.stdin_channel.socket
        poller.register(stdin_socket, zmq.POLLIN)

        while len(self.pending) > 0:
            if timeout is not None:
                timeout = max(0, deadline - time.monotonic())
                timeout_ms = int(1000 * timeout)

            events = dict(poller.poll(timeout_ms))
            print(events)

            if not events:
                raise TimeoutError("Timeout waiting for output")

            if stdin_socket in events:
                msg = self.client.get_stdin_msg()
                self.validate_message(msg, 'stdin')
                if ("msg_id" in msg["parent_header"]
                    and msg["parent_header"]["msg_id"] in self.pending):
                    assert stdin_hook is not None, "Input request received but no hook available."
                    stdin_hook(msg)
            if shell_socket in events:
                msg = self.client.get_shell_msg()
                self.validate_message(msg, 'shell')
                if ("msg_id" in msg["parent_header"]
                    and msg["parent_header"]["msg_id"] in self.pending):
                    parent_msg_id = msg["parent_header"]["msg_id"]
                    assert self.pending[parent_msg_id] == "shell", "Received response on shell channel for a control message."
                    replies[parent_msg_id] = msg
                    if parent_msg_id in idle:
                        del self.pending[parent_msg_id]
            if control_socket in events:
                msg = self.client.get_control_msg()
                self.validate_message(msg, 'control')
                if ("msg_id" in msg["parent_header"]
                    and msg["parent_header"]["msg_id"] in self.pending):
                    parent_msg_id = msg["parent_header"]["msg_id"]
                    assert self.pending[parent_msg_id] == "control", "Received response on control channel for a shell message."
                    replies[parent_msg_id] = msg
                    if parent_msg_id in idle:
                        del self.pending[parent_msg_id]
            if iopub_socket in events:
                msg = self.client.get_iopub_msg()
                self.validate_message(msg, 'iopub')
                if ("msg_id" in msg["parent_header"]
                    and msg["parent_header"]["msg_id"] in self.pending):
                    if msg["parent_header"] is None or "msg_id" not in msg["parent_header"]:
                        continue
                    parent_msg_id = msg["parent_header"]["msg_id"]
                    if parent_msg_id not in messages:
                        assert (msg["header"]["msg_type"] == "status"
                            and msg["content"]["execution_state"] == "busy")
                        messages[parent_msg_id] = [msg] if keep_status else []
                    elif (
                        msg["header"]["msg_type"] == "status"
                        and msg["content"]["execution_state"] == "idle"
                    ):
                        idle[parent_msg_id] = True
                        if parent_msg_id in replies:
                            del self.pending[parent_msg_id]
                        if keep_status:
                            messages[parent_msg_id].append(msg)
                    else:
                        messages[parent_msg_id].append(msg)

        return replies, messages

    def shutdown(self):
        msg_id = self.client.shutdown()
        self.pending[msg_id] = "control"
        return msg_id

    def interrupt(self):
        msg = self.client.session.msg("interrupt_request", {})
        self.client.control_channel.send(msg)
        msg_id = msg["header"]["msg_id"]
        self.pending[msg_id] = "control"
        return msg_id

    def execute(self, code, silent = False, store_history = True,
                user_expressions = None, allow_stdin = False,
                stop_on_error = True):
        msg_id = self.client.execute(code, silent=silent,
                                     store_history=store_history, user_expressions=user_expressions,
                                     allow_stdin=allow_stdin, stop_on_error=stop_on_error)
        self.pending[msg_id] = "shell"
        return msg_id

    def complete(self, code, cursor_pos = None):
        msg_id = self.client.complete(code, cursor_pos=cursor_pos)
        self.pending[msg_id] = "shell"
        return msg_id

    def inspect(self, code, cursor_pos = None, detail_level = 0):
        msg_id = self.client.inspect(code, cursor_pos=cursor_pos,
                                     detail_level=detail_level)
        self.pending[msg_id] = "shell"
        return msg_id

    def history(self, raw = True, output = False, hist_access_type = "range", **kwargs):
        msg_id = self.client.history(raw = raw, output = output,
                                     history_access_type = history_access_type,
                                    **kwargs)
        self.pending[msg_id] = "shell"
        return msg_id

    def kernel_info(self):
        msg_id = self.client.kernel_info()
        self.pending[msg_id] = "shell"
        return msg_id

    def comm_info(self, target_name = None):
        msg_id = self.client.comm_info(target_name = target_name)
        self.pending[msg_id] = "shell"
        return msg_id

    def is_complete(self, code):
        msg_id = self.client.is_complete(code)
        self.pending[msg_id] = "shell"
        return msg_id

    def input(self, string):
        self.client.input(string)

    def execute_read_reply(self, code, silent = False, store_history = True,
                           user_expressions = None, stop_on_error = True,
                           timeout = None, stdin_hook = None,
                           keep_status = False):
        msg_id = self.execute(code, silent = silent,
                              store_history = store_history,
                              user_expressions = user_expressions,
                              stop_on_error = stop_on_error,
                              allow_stdin = stdin_hook is not None)
        replies, messages = self.read_replies(timeout = timeout)
        assert (len(replies) == 1 and msg_id in replies
                and replies[msg_id]["msg_type"] == "execute_reply"
                and replies[msg_id]["content"]["status"] == "ok")
        assert any(msg['msg_type'] == 'execute_input' and msg['content']['code'] == code for msg in messages[msg_id])
        return replies[msg_id], messages[msg_id] if msg_id in messages else []

    def complete_read_reply(self, code, cursor_pos = None, timeout = None):
        msg_id = self.complete(code, cursor_pos = cursor_pos)
        replies, messages = self.read_replies(timeout = timeout)
        assert (len(replies) == 1 and msg_id in replies
                and replies[msg_id]["msg_type"] == "complete_reply"
                and replies[msg_id]["content"]["status"] == "ok")
        return replies[msg_id], messages[msg_id] if msg_id in messages else []

    def inspect_read_reply(self, code, cursor_pos = None, detail_level = 0,
                     timeout = None):
        msg_id = self.inspect(code, cursor_pos = cursor_pos,
                              detail_level = detail_level)
        replies, messages = self.read_replies(timeout = timeout)
        assert (len(replies) == 1 and msg_id in replies
                and replies[msg_id]["msg_type"] == "inspect_reply"
                and replies[msg_id]["content"]["status"] == "ok")
        return replies[msg_id], messages[msg_id] if msg_id in messages else []

    def history_read_reply(self, raw = True, output = False, timeout = None,
                     hist_access_type = "range", **kwargs):
        msg_id = self.history(raw = raw, output = output,
                              hist_access_type = hist_access_type,
                              **kwargs)
        replies, messages = self.read_replies(timeout = timeout)
        assert (len(replies) == 1 and msg_id in replies
                and replies[msg_id]["msg_type"] == "history_reply"
                and replies[msg_id]["content"]["status"] == "ok")
        return replies[msg_id], messages[msg_id] if msg_id in messages else []

    def kernel_info_read_reply(self, timeout = None):
        msg_id = self.kernel_info()
        replies, messages = self.read_replies(timeout = timeout)
        assert (len(replies) == 1 and msg_id in replies
                and replies[msg_id]["msg_type"] == "kernel_info_reply"
                and replies[msg_id]["content"]["status"] == "ok")
        return replies[msg_id], messages[msg_id] if msg_id in messages else []

    def comm_info_read_reply(self, target_name = None, timeout = None):
        msg_id = self.comm_info(target_name = target_name)
        replies, messages = self.read_replies(timeout = timeout)
        assert (len(replies) == 1 and msg_id in replies
                and replies[msg_id]["msg_type"] == "comm_info_reply"
                and replies[msg_id]["content"]["status"] == "ok")
        return replies[msg_id], messages[msg_id] if msg_id in messages else []

    def is_complete_read_reply(self, code, timeout = None):
        msg_id = self.is_complete(code)
        replies, messages = self.read_replies(timeout = timeout)
        assert (len(replies) == 1 and msg_id in replies
                and replies[msg_id]["msg_type"] == "is_complete_reply")
        return replies[msg_id], messages[msg_id] if msg_id in messages else []


@pytest.fixture(params=["common-lisp_sbcl"])
def jupyter_kernel(request):
    kernel = Kernel(request.param)
    kernel.start()
    yield kernel
    kernel.shutdown()


def test_execute(jupyter_kernel):
    reply, messages = jupyter_kernel.execute_read_reply("(1+ 7)", timeout = 10)
    assert any(msg['msg_type'] == 'execute_result' and msg['content']['data']['text/plain'] == '8' for msg in messages), "wibble"


def test_kernel_info(jupyter_kernel):
    reply, messages = jupyter_kernel.kernel_info_read_reply(timeout = 10)
    assert reply['content']['implementation'] == 'common-lisp'


def test_comm_info(jupyter_kernel):
    reply, messages = jupyter_kernel.comm_info_read_reply(timeout = 10)


def test_is_complete(jupyter_kernel):
    reply, messages = jupyter_kernel.is_complete_read_reply("(fu bar)", timeout = 10)
    assert reply['content']['status'] == 'complete'

    reply, messages = jupyter_kernel.is_complete_read_reply("(fu bar", timeout = 10)
    assert reply['content']['status'] == 'incomplete'

