import json
import jsonschema
import jupyter_client
import os
import pytest
import time
import zmq


def dictionary_matches(needle, haystack):
    return all(
        key in haystack and haystack[key] == value for key, value in needle.items()
    )


def check_message_contents(msg_type, needles, messages):
    haystacks = [msg["content"] for msg in messages if msg["msg_type"] == msg_type]
    assert len(needles) == len(
        haystacks
    ), f"Expected to receive {len(needles)} {msg_type} messages but received {len(haystacks)} instead."
    for (needle, haystack) in zip(needles, haystacks):
        assert dictionary_matches(
            needle, haystack
        ), f"Expected a {msg_type} of {needle} but received {haystack} instead."


def check_array_contents(needles, haystacks):
    assert len(needles) == len(
        haystacks
    ), f"Expected to receive {len(needles)} items but received {len(haystacks)} instead."
    for (needle, haystack) in zip(needles, haystacks):
        assert dictionary_matches(
            needle, haystack
        ), f"Expected {needle} but received {haystack} instead."


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
                print(events)

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
                            if parent_msg_id in replies:
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
        expected_reply_status=None,
        expected_reply_type=None,
        expected_reply_ename=None,
        expected_reply_evalue=None,
        expected_reply_traceback=None,
        expected_stream=None,
    ):
        replies, messages = self.read_replies(
            timeout=timeout, stdin_hook=stdin_hook, keep_status=keep_status
        )
        assert (
            len(replies) == 1 and msg_id in replies
        ), "Expected a single reply with a msg_id of {msg_id}."
        my_reply = replies[msg_id]
        my_messages = messages[msg_id] if msg_id in messages else []
        if expected_reply_type is not None:
            assert (
                my_reply["msg_type"] == expected_reply_type
            ), 'Expected a message_type of "{expected_reply_type}" but received "{my_reply["msg_type"]}" instead.'
        if expected_reply_status is not None:
            assert (
                my_reply["content"]["status"] == expected_reply_status
            ), f'Expected a reply status of "{expected_reply_status}" but received "{my_reply["content"]["status"]}" instead.'
        if expected_reply_ename is not None:
            assert (
                my_reply["content"]["ename"] == expected_reply_ename
            ), f'Expected a reply ename of "{expected_reply_ename}" but received "{my_reply["content"]["ename"]}" instead.'
        if expected_reply_evalue is not None:
            assert (
                my_reply["content"]["evalue"] == expected_reply_evalue
            ), f'Expected a reply evalue of "{expected_reply_evalue}" but received "{my_reply["content"]["evalue"]}" instead.'
        if expected_reply_traceback is not None:
            assert (
                my_reply["content"]["traceback"] == expected_reply_traceback
            ), f'Expected a reply traceback of "{expected_reply_traceback}" but received "{my_reply["content"]["traceback"]}" instead.'
        if expected_stream is not None:
            check_message_contents("stream", expected_stream, my_messages)
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
            raw=raw, output=output, history_access_type=history_access_type, **kwargs
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
        expected_reply_status=None,
        expected_reply_ename=None,
        expected_reply_evalue=None,
        expected_reply_traceback=None,
        expected_reply_payload=None,
        expected_stream=None,
        expected_execute_result=None,
        expected_display_data=None,
        expected_update_display_data=None,
        expected_clear_output=None,
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
            expected_reply_type="execute_reply",
            expected_reply_status=expected_reply_status,
            expected_reply_ename=expected_reply_ename,
            expected_reply_evalue=expected_reply_evalue,
            expected_reply_traceback=expected_reply_traceback,
            expected_stream=expected_stream,
        )
        assert any(
            msg["msg_type"] == "execute_input" and msg["content"]["code"] == code
            for msg in messages
        ), "Expected an execute_input message with the correct code content."
        if expected_reply_payload is not None:
            assert "payload" in reply["content"], "Expected a payload in execute_reply."
            check_array_contents(expected_reply_payload, reply["content"]["payload"])
        if expected_execute_result is not None:
            check_message_contents("execute_result", expected_execute_result, messages)
        if expected_display_data is not None:
            check_message_contents("display_data", expected_display_data, messages)
        if expected_update_display_data is not None:
            check_message_contents(
                "update_display_data", expected_update_display_data, messages
            )
        if expected_clear_output is not None:
            check_message_contents("clear_output", expected_clear_output, messages)
        return reply, messages

    def complete_read_reply(
        self,
        code,
        cursor_pos=None,
        timeout=None,
        expected_matches=None,
        expected_cursor_start=None,
        expected_cursor_end=None,
        expected_reply_status=None,
        expected_reply_ename=None,
        expected_reply_evalue=None,
        expected_reply_traceback=None,
        expected_stream=None,
    ):
        msg_id = self.complete(code, cursor_pos=cursor_pos)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_reply_type="complete_reply",
            expected_reply_status=expected_reply_status,
            expected_reply_ename=expected_reply_ename,
            expected_reply_evalue=expected_reply_evalue,
            expected_reply_traceback=expected_reply_traceback,
            expected_stream=expected_stream,
        )
        if expected_matches is not None:
            for match in expected_matches:
                assert any(
                    text == match["text"] for text in reply["content"]["matches"]
                ), f'Expected a match of "{match["text"]}" in matches.'
                if "type" in match:
                    assert (
                        "_jupyter_types_experimental" in reply["content"]["metadata"]
                    ), 'Match types expected in reply, but no key "_jupyter_types_experimental" found in metadata.'
                    t = next(
                        (
                            t
                            for t in reply["content"]["metadata"][
                                "_jupyter_types_experimental"
                            ]
                            if t["text"] == match["text"]
                        ),
                        None,
                    )
                    assert (
                        t is not None
                    ), 'Expected a type entry for the text "{match["text"]}".'
                    assert (
                        t["type"] == match["type"]
                    ), f'Expected the type of "{match["text"]}" to be "{match["type"]}" but found "{t["type"]}" instead.'
        if expected_cursor_start is not None:
            assert (
                reply["content"]["cursor_start"] == expected_cursor_start
            ), f'Expected a cursor_start of {expected_cursor_start} but received {reply["content"]["cursor_start"]}.'
        if expected_cursor_end is not None:
            assert (
                reply["content"]["cursor_end"] == expected_cursor_end
            ), f'Expected a cursor_end of {expected_cursor_end} but received {reply["content"]["cursor_end"]}.'
        return reply, messages

    def inspect_read_reply(
        self,
        code,
        cursor_pos=None,
        detail_level=0,
        timeout=None,
        expected_reply_status=None,
        expected_reply_ename=None,
        expected_reply_evalue=None,
        expected_reply_traceback=None,
        expected_stream=None,
    ):
        msg_id = self.inspect(code, cursor_pos=cursor_pos, detail_level=detail_level)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_reply_type="inspect_reply",
            expected_reply_status=expected_reply_status,
            expected_reply_ename=expected_reply_ename,
            expected_reply_evalue=expected_reply_evalue,
            expected_reply_traceback=expected_reply_traceback,
            expected_stream=expected_stream,
        )
        return reply, messages

    def history_read_reply(
        self,
        raw=True,
        output=False,
        timeout=None,
        hist_access_type="range",
        expected_reply_status=None,
        expected_reply_ename=None,
        expected_reply_evalue=None,
        expected_reply_traceback=None,
        expected_stream=None,
        **kwargs,
    ):
        msg_id = self.history(
            raw=raw, output=output, hist_access_type=hist_access_type, **kwargs
        )
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_reply_type="history_reply",
            expected_reply_status=expected_reply_status,
            expected_reply_ename=expected_reply_ename,
            expected_reply_evalue=expected_reply_evalue,
            expected_reply_traceback=expected_reply_traceback,
            expected_stream=expected_stream,
        )
        return reply, messages

    def kernel_info_read_reply(
        self,
        timeout=None,
        expected_reply_status=None,
        expected_reply_ename=None,
        expected_reply_evalue=None,
        expected_reply_traceback=None,
        expected_stream=None,
    ):
        msg_id = self.kernel_info()
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_reply_type="kernel_info_reply",
            expected_reply_status=expected_reply_status,
            expected_reply_ename=expected_reply_ename,
            expected_reply_evalue=expected_reply_evalue,
            expected_reply_traceback=expected_reply_traceback,
            expected_stream=expected_stream,
        )
        return reply, messages

    def comm_info_read_reply(
        self,
        target_name=None,
        timeout=None,
        expected_reply_status=None,
        expected_reply_ename=None,
        expected_reply_evalue=None,
        expected_reply_traceback=None,
        expected_stream=None,
    ):
        msg_id = self.comm_info(target_name=target_name)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_reply_type="comm_info_reply",
            expected_reply_status=expected_reply_status,
            expected_reply_ename=expected_reply_ename,
            expected_reply_evalue=expected_reply_evalue,
            expected_reply_traceback=expected_reply_traceback,
            expected_stream=expected_stream,
        )
        return reply, messages

    def is_complete_read_reply(
        self,
        code,
        timeout=None,
        expected_reply_status=None,
        expected_indent=None,
        expected_stream=None,
    ):
        msg_id = self.is_complete(code)
        reply, messages = self.read_reply(
            msg_id,
            timeout=timeout,
            expected_reply_type="is_complete_reply",
            expected_reply_status=expected_reply_status,
            expected_stream=expected_stream,
        )
        if expected_indent is not None:
            assert (
                reply["content"]["indent"] == expected_indent
            ), f'Expected an reply with an indent of "{expected_indent}" but received "{reply["content"]["indent"]}" instead.'
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
