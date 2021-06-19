import json
import jsonschema
import jupyter_client
import os
import pytest
import pprint
import time
import zmq


def matches(needle, haystack):
    if isinstance(needle, type):
        return isinstance(haystack, needle)
    if isinstance(needle, dict):
        if not isinstance(haystack, dict):
            return False
        for key, value in needle.items():
            if key not in haystack or not matches(value, haystack[key]):
                return False
        return True
    if isinstance(needle, list):
        if not isinstance(haystack, list) or len(needle) != len(haystack):
            return False
        for (n, h) in zip(needle, haystack):
            if not matches(n, h):
                return False
        return True
    if isinstance(needle, (set, tuple)):
        for n in needle:
            if not any(matches(n, h) for h in haystack):
                return False
        return True
    return needle == haystack


def assert_matches(needle, haystack, env, ref):
    if isinstance(needle, type):
        assert isinstance(
            haystack, needle
        ), f"Expected {ref} in the following message to be of type {pprint.pformat(needle)}.\n\t{pprint.pformat(env)}"
    elif isinstance(needle, dict):
        assert isinstance(
            haystack, dict
        ), f"Expected {ref} in the following message to be an object.\n\t{pprint.pformat(env)}"
        for key, value in needle.items():
            assert (
                key in haystack
            ), f"Expected {ref} to be present following message.\n\t{pprint.pformat(env)}"
            assert_matches(value, haystack[key], env, f'{ref}["{key}"]')
    elif isinstance(needle, list):
        assert isinstance(
            haystack, list
        ), f"Expected {ref} in the following message to be an array.\n\t{pprint.pformat(env)}"
        assert len(needle) == len(
            haystack
        ), f"Expected {ref} in the following message to have a length of {len(haystack)}.\n\t{pprint.pformat(env)}"
        for (i, n, h) in zip(range(len(needle)), needle, haystack):
            assert_matches(n, h, env, f"{ref}[{i}]")
    elif isinstance(needle, (set, tuple)):
        for n in needle:
            assert any(
                matches(n, h) for h in haystack
            ), f"Expected {ref} in\n\t{pprint.pformat(env)}\nto contain\n\t{pprint.pformat(n)}"
    else:
        assert (
            needle == haystack
        ), f"Expected {ref} in\n\t{pprint.pformat(env)}\nto equal\n\t{pprint.pformat(needle)}"


class Kernel(object):
    def __init__(self, kernel_name):
        self.kernel = jupyter_client.KernelManager(kernel_name=kernel_name)
        self.pending = {}
        with open(os.path.join(os.path.dirname(__file__), "message-schema.json")) as f:
            message_schema = json.load(f)
            jsonschema.Draft7Validator.check_schema(message_schema)
            self.message_validator = jsonschema.Draft7Validator(message_schema)

    def start(self):
        self.kernel.start_kernel()
        self.client = self.kernel.client()
        self.client.start_channels()
        self.client.wait_for_ready()

    def restart(self):
        self.kernel.restart_kernel(now=True)
        self.client = self.kernel.client()
        self.client.start_channels()
        self.client.wait_for_ready()

    def validate_message(self, msg, source):
        msg_type = msg["header"]["msg_type"]
        self.message_validator.validate(msg)
        # if msg["parent_header"] is not None and "msg_id" in msg["parent_header"]:
        #    assert msg["parent_header"]["msg_id"] in self.pending, "Unknown parent message id."

    def read_replies(self, timeout=None, stdin_hook=None, keep_status=False):
        try:
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

                if not events:
                    raise TimeoutError("Timeout waiting for output")

                if stdin_socket in events:
                    msg = self.client.get_stdin_msg()
                    self.validate_message(msg, "stdin")
                    if (
                        "msg_id" in msg["parent_header"]
                        and msg["parent_header"]["msg_id"] in self.pending
                    ):
                        assert (
                            stdin_hook is not None
                        ), "Input request received but no hook available."
                        stdin_hook(msg)
                if shell_socket in events:
                    msg = self.client.get_shell_msg()
                    self.validate_message(msg, "shell")
                    if (
                        "msg_id" in msg["parent_header"]
                        and msg["parent_header"]["msg_id"] in self.pending
                    ):
                        parent_msg_id = msg["parent_header"]["msg_id"]
                        assert (
                            self.pending[parent_msg_id] == "shell"
                        ), "Received response on shell channel for a control message."
                        replies[parent_msg_id] = msg
                        if parent_msg_id in idle:
                            del self.pending[parent_msg_id]
                if control_socket in events:
                    msg = self.client.get_control_msg()
                    self.validate_message(msg, "control")
                    if (
                        "msg_id" in msg["parent_header"]
                        and msg["parent_header"]["msg_id"] in self.pending
                    ):
                        parent_msg_id = msg["parent_header"]["msg_id"]
                        assert (
                            self.pending[parent_msg_id] == "control"
                        ), "Received response on control channel for a shell message."
                        replies[parent_msg_id] = msg
                        if parent_msg_id in idle:
                            del self.pending[parent_msg_id]
                if iopub_socket in events:
                    msg = self.client.get_iopub_msg()
                    self.validate_message(msg, "iopub")
                    if (
                        "msg_id" in msg["parent_header"]
                        and msg["parent_header"]["msg_id"] in self.pending
                    ):
                        if (
                            msg["parent_header"] is None
                            or "msg_id" not in msg["parent_header"]
                        ):
                            continue
                        parent_msg_id = msg["parent_header"]["msg_id"]
                        if parent_msg_id not in messages:
                            assert (
                                msg["header"]["msg_type"] == "status"
                                and msg["content"]["execution_state"] == "busy"
                            )
                            messages[parent_msg_id] = [msg] if keep_status else []
                        elif (
                            msg["header"]["msg_type"] == "status"
                            and msg["content"]["execution_state"] == "idle"
                        ):
                            idle[parent_msg_id] = True
                            if (
                                parent_msg_id in replies
                                or self.pending[parent_msg_id] is "iopub"
                            ):
                                del self.pending[parent_msg_id]
                            if keep_status:
                                messages[parent_msg_id].append(msg)
                        else:
                            messages[parent_msg_id].append(msg)

            return replies, messages
        except:
            self.restart()
            raise

    def read_reply(
        self,
        msg_id,
        timeout=None,
        stdin_hook=None,
        keep_status=False,
        expected_reply=None,
        expected_messages=None,
        need_reply=False,
    ):
        replies, messages = self.read_replies(
            timeout=timeout, stdin_hook=stdin_hook, keep_status=keep_status
        )
        if need_reply:
            assert (
                len(replies) == 1 and msg_id in replies
            ), "Expected a single reply with a msg_id of {msg_id}."
        else:
            assert len(replies) == 0, "Expected no replies."
        my_reply = replies[msg_id] if msg_id in replies else None
        my_messages = messages[msg_id] if msg_id in messages else []
        if expected_reply is not None:
            for expected in expected_reply:
                assert_matches(expected, my_reply, my_reply, "message")
        if expected_messages is not None:
            for expected_group in expected_messages:
                index = 0
                for message in my_messages:
                    if index >= len(expected_group):
                        break
                    if matches(expected_group[index], message):
                        index += 1
                assert index == len(
                    expected_group
                ), f"Not able to match the messages {expected_group} to {my_messages}."
        return my_reply, my_messages

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

    def execute(
        self,
        code,
        silent=False,
        store_history=True,
        user_expressions=None,
        allow_stdin=False,
        stop_on_error=True,
    ):
        msg_id = self.client.execute(
            code,
            silent=silent,
            store_history=store_history,
            user_expressions=user_expressions,
            allow_stdin=allow_stdin,
            stop_on_error=stop_on_error,
        )
        self.pending[msg_id] = "shell"
        return msg_id

    def complete(self, code, cursor_pos=None):
        msg_id = self.client.complete(code, cursor_pos=cursor_pos)
        self.pending[msg_id] = "shell"
        return msg_id

    def inspect(self, code, cursor_pos=None, detail_level=0):
        msg_id = self.client.inspect(
            code, cursor_pos=cursor_pos, detail_level=detail_level
        )
        self.pending[msg_id] = "shell"
        return msg_id

    def history(self, raw=True, output=False, hist_access_type="range", **kwargs):
        msg_id = self.client.history(
            raw=raw, output=output, hist_access_type=hist_access_type, **kwargs
        )
        self.pending[msg_id] = "shell"
        return msg_id

    def kernel_info(self):
        msg_id = self.client.kernel_info()
        self.pending[msg_id] = "shell"
        return msg_id

    def comm_info(self, target_name=None):
        msg_id = self.client.comm_info(target_name=target_name)
        self.pending[msg_id] = "shell"
        return msg_id

    def comm_open(self, comm_id=None, target_name=None, data=None):
        msg = self.client.session.msg(
            "comm_open", dict(comm_id=comm_id, target_name=target_name, data=data)
        )
        msg_id = msg["header"]["msg_id"]
        self.client.shell_channel.send(msg)
        self.pending[msg_id] = "iopub"
        return msg_id

    def comm_msg(self, comm_id=None, data=None):
        msg = self.client.session.msg("comm_msg", dict(comm_id=comm_id, data=data))
        msg_id = msg["header"]["msg_id"]
        self.client.shell_channel.send(msg)
        self.pending[msg_id] = "iopub"
        return msg_id

    def comm_close(self, comm_id=None, data=None):
        msg = self.client.session.msg("comm_close", dict(comm_id=comm_id, data=data))
        msg_id = msg["header"]["msg_id"]
        self.client.shell_channel.send(msg)
        self.pending[msg_id] = "iopub"
        return msg_id

    def is_complete(self, code):
        msg_id = self.client.is_complete(code)
        self.pending[msg_id] = "shell"
        return msg_id

    def input(self, string):
        self.client.input(string)

    def execute_read_reply(
        self,
        code,
        silent=False,
        store_history=True,
        user_expressions=None,
        stop_on_error=True,
        timeout=None,
        stdin_hook=None,
        keep_status=False,
        expected_reply=None,
        expected_messages=None,
    ):
        msg_id = self.execute(
            code,
            silent=silent,
            store_history=store_history,
            user_expressions=user_expressions,
            stop_on_error=stop_on_error,
            allow_stdin=stdin_hook is not None,
        )
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            need_reply=True,
            expected_reply=([] if expected_reply is None else expected_reply)
            + [{"msg_type": "execute_reply"}],
            expected_messages=([] if expected_messages is None else expected_messages)
            + [[{"msg_type": "execute_input", "content": {"code": code}}]],
        )
        return reply, messages

    def complete_read_reply(
        self,
        code,
        cursor_pos=None,
        timeout=None,
        expected_reply=None,
        expected_messages=None,
    ):
        msg_id = self.complete(code, cursor_pos=cursor_pos)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            need_reply=True,
            expected_reply=([] if expected_reply is None else expected_reply)
            + [{"msg_type": "complete_reply"}],
            expected_messages=expected_messages,
        )
        return reply, messages

    def inspect_read_reply(
        self,
        code,
        cursor_pos=None,
        detail_level=0,
        timeout=None,
        expected_reply=None,
        expected_messages=None,
    ):
        msg_id = self.inspect(code, cursor_pos=cursor_pos, detail_level=detail_level)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            need_reply=True,
            expected_reply=([] if expected_reply is None else expected_reply)
            + [{"msg_type": "inspect_reply"}],
            expected_messages=expected_messages,
        )
        return reply, messages

    def history_read_reply(
        self,
        raw=True,
        output=False,
        timeout=None,
        hist_access_type="range",
        expected_reply=None,
        expected_messages=None,
        **kwargs,
    ):
        msg_id = self.history(
            raw=raw, output=output, hist_access_type=hist_access_type, **kwargs
        )
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            need_reply=True,
            expected_reply=([] if expected_reply is None else expected_reply)
            + [{"msg_type": "history_reply"}],
            expected_messages=expected_messages,
        )
        return reply, messages

    def kernel_info_read_reply(
        self,
        timeout=None,
        expected_reply=None,
        expected_messages=None,
    ):
        msg_id = self.kernel_info()
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            need_reply=True,
            expected_reply=([] if expected_reply is None else expected_reply)
            + [{"msg_type": "kernel_info_reply"}],
            expected_messages=expected_messages,
        )
        return reply, messages

    def comm_info_read_reply(
        self,
        target_name=None,
        timeout=None,
        expected_reply=None,
        expected_messages=None,
    ):
        msg_id = self.comm_info(target_name=target_name)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            need_reply=True,
            expected_reply=([] if expected_reply is None else expected_reply)
            + [{"msg_type": "comm_info_reply"}],
            expected_messages=expected_messages,
        )
        return reply, messages

    def comm_open_read_reply(
        self,
        comm_id=None,
        target_name=None,
        data=None,
        timeout=None,
        expected_messages=None,
    ):
        msg_id = self.comm_open(comm_id=comm_id, target_name=target_name, data=data)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_messages=expected_messages,
        )
        return messages

    def comm_msg_read_reply(
        self, comm_id=None, data=None, timeout=None, expected_messages=None
    ):
        msg_id = self.comm_msg(comm_id=comm_id, data=data)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_messages=expected_messages,
        )
        return messages

    def comm_close_read_reply(
        self, comm_id=None, data=None, timeout=None, expected_messages=None
    ):
        msg_id = self.comm_close(comm_id=comm_id, data=data)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_messages=expected_messages,
        )
        return messages

    def is_complete_read_reply(
        self,
        code,
        timeout=None,
        expected_reply=None,
        expected_messages=None,
    ):
        msg_id = self.is_complete(code)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            need_reply=True,
            expected_reply=([] if expected_reply is None else expected_reply)
            + [{"msg_type": "is_complete_reply"}],
            expected_messages=expected_messages,
        )
        return reply, messages


@pytest.fixture(params=["python3"], scope="module")
def jupyter_kernel(request):
    try:
        kernel = Kernel(request.param)
        kernel.start()
        yield kernel
        kernel.shutdown()
    except jupyter_client.kernelspec.NoSuchKernel:
        pytest.skip(
            f"Skipping tests for {request.param} kernel since it is not present."
        )
