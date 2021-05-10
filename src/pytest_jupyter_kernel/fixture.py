import json
import jsonschema
import jupyter_client
import os
import pytest
import time
import zmq


class Kernel(object):
    def __init__(self, kernel_name):
        self.kernel = jupyter_client.KernelManager(kernel_name=kernel_name)
        self.pending = {}
        self.schema = {}
        root = os.path.join(os.path.dirname(__file__), 'schema')
        for file in os.listdir(root):
            name, ext = os.path.splitext(os.path.basename(file))
            with open(os.path.join(root, file)) as f:
                self.schema[name] = json.load(f)
        print(self.schema)

    def start(self):
        self.kernel.start_kernel()
        self.client = self.kernel.client()
        self.client.start_channels()

    def shutdown(self):
        self.client.shutdown()

    def interrupt(self):
        self.kernel.interrupt_kernel()

    def validate_message(self, msg, source):
        parent_msg_id = msg["parent_header"]["msg_id"]
        msg_type = msg['header']['msg_type']
        print(msg_type)
        print(msg['parent_header']['msg_id'])
        print(msg_type in self.schema)
        assert parent_msg_id in self.pending, "Unknown parent message id."
        if msg_type in self.schema:
            jsonschema.validate(msg['content'], self.schema[msg_type])

    def read_replies(self, timeout=None, stdin_hook=None, keep_status=False):
        messages = {}
        replies = {}

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

            if not events:
                print(messages)
                print(replies)
                raise TimeoutError("Timeout waiting for output")

            if stdin_socket in events:
                msg = self.client.get_stdin_msg()
                self.validate_message(msg, 'stdin')
                assert stdin_hook is not None, "Input request received but no hook available."
                stdin_hook(msg)
            elif shell_socket in events:
                msg = self.client.get_shell_msg()
                self.validate_message(msg, 'shell')
                parent_msg_id = msg["parent_header"]["msg_id"]
                assert self.pending[parent_msg_id] == "shell", "Received response on shell channel for a control message."
                replies[parent_msg_id] = msg
            elif control_socket in events:
                msg = self.client.get_control_msg()
                self.validate_message(msg, 'control')
                parent_msg_id = msg["parent_header"]["msg_id"]
                assert self.pending[parent_msg_id] == "control", "Received response on control channel for a shell message."
                replies[parent_msg_id] = msg
            elif iopub_socket in events:
                msg = self.client.get_iopub_msg()
                self.validate_message(msg, 'iopub')
                if msg["parent_header"] is None:
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
                    del self.pending[parent_msg_id]
                    if keep_status:
                        messages[parent_msg_id].append(msg)
                else:
                    messages[parent_msg_id].append(msg)

        return replies, messages

    def execute(self, code, silent = False, store_history = True,
        user_expressions = None, allow_stdin = False, stop_on_error = True):
        self.pending[self.client.execute(code, silent=silent,
            store_history=store_history, user_expressions=user_expressions,
            allow_stdin=allow_stdin, stop_on_error=stop_on_error)] = "shell"

    def complete(self, code, cursor_pos = None):
        self.pending[self.client.complete(code, cursor_pos=cursor_pos)] = "shell"

    def inspect(self, code, cursor_pos = None, detail_level = 0):
        self.pending[self.client.inspect(code, cursor_pos=cursor_pos,
                                         detail_level=detail_level)] = "shell"

    def history(self, raw = True, output = False, hist_access_type = "range", **kwargs):
        self.pending[self.client.history(raw = raw, output = output,
                                         history_access_type = history_access_type,
                                         **kwargs)] = "shell"

    def kernel_info(self):
        self.pending[self.client.kernel_info()] = "shell"

    def comm_info(self, target_name = None):
        self.pending[self.client.comm_info(target_name = target_name)] = "shell"

    def is_complete(self, code):
        self.pending[self.client.is_complete(code)] = "shell"

    def input(self, string):
        self.client.input(string)


@pytest.fixture
def jupyter_kernel():
    kernel = Kernel("common-lisp_sbcl")
    kernel.start()
    yield kernel
    kernel.shutdown()


def test_execute(jupyter_kernel):
    jupyter_kernel.execute("(1+ 7)")
    replies, messages = jupyter_kernel.read_replies(timeout = 10)
    print(replies)
    print(messages)
    assert 1 == 0, "wibble"


def test_kernel_info(jupyter_kernel):
    jupyter_kernel.kernel_info()
    replies, messages = jupyter_kernel.read_replies(timeout = 10)
    print(replies)
    print(messages)
    assert 1 == 0    
