"""The wire rules: method names, envelopes, error codes, versions, extensions.

Everything here is a claim about the A2A v1.0 specification, so each test says
which section it is asserting. The two facts that were checked against
`specification/a2a.proto` and the binding sections rather than against
secondary sources -- the bare PascalCase method names and the
`/.well-known/agent-card.json` path -- are pinned first, because a change to
either silently breaks interoperability with every conformant peer while every
other test in this file keeps passing.
"""

from __future__ import annotations

import json

import pytest

from psych_runtime.a2a.errors import (
    A2AError,
    ExtensionSupportRequiredError,
    InvalidParamsError,
    InvalidRequestError,
    JsonParseError,
    MethodNotFoundError,
    TaskNotFoundError,
    VersionNotSupportedError,
    jsonrpc_error_object,
    rpc_status_body,
)
from psych_runtime.a2a.jsonrpc import (
    METHOD_PARAMS,
    STREAMING_METHODS,
    A2AMethod,
    error_response,
    parse_body,
    parse_request,
    stream_frame,
    success_response,
)
from psych_runtime.a2a.models import (
    AGENT_CARD_WELL_KNOWN_PATH,
    INTERRUPTED_STATES,
    TERMINAL_STATES,
    AgentExtension,
    Message,
    Part,
    Role,
    SendMessageRequest,
    StreamResponse,
    Task,
    TaskState,
    TaskStatus,
    wire_dict,
)
from psych_runtime.a2a.negotiation import (
    ABSENT_VERSION,
    PROTOCOL_VERSION,
    activated_extensions,
    negotiate_version,
    parse_extensions_header,
    require_declared_extensions,
)
from psych_runtime.a2a.rest import (
    REST_ROUTES,
    error_body,
    get_task_request_from_query,
    list_tasks_request_from_query,
    sse_frame,
)

pytestmark = pytest.mark.unit


class TestTheTwoFactsResolvedFromTheProto:
    def test_method_names_are_bare_pascal_case(self) -> None:
        """§9.1, §9.4.1 and §5.3's mapping table. Not `a2a.SendMessage`, and
        not the pre-1.0 `message/send`."""
        assert A2AMethod.SEND_MESSAGE.value == "SendMessage"
        assert A2AMethod.GET_TASK.value == "GetTask"
        assert A2AMethod.CREATE_TASK_PUSH_NOTIFICATION_CONFIG.value == (
            "CreateTaskPushNotificationConfig"
        )
        assert not any("/" in method.value or "." in method.value for method in A2AMethod)

    def test_the_well_known_card_path(self) -> None:
        """§8.2 and the IANA registration in §14."""
        assert AGENT_CARD_WELL_KNOWN_PATH == "/.well-known/agent-card.json"

    def test_all_eleven_core_operations_are_present(self) -> None:
        """§3.1 lists eleven, and §5.1 requires every binding to offer all of
        them."""
        assert len(A2AMethod) == 11
        assert set(METHOD_PARAMS) == set(A2AMethod)
        assert set(REST_ROUTES) == set(A2AMethod)


class TestTaskStateSets:
    def test_terminal_and_interrupted_are_disjoint_and_complete(self) -> None:
        assert not (TERMINAL_STATES & INTERRUPTED_STATES)
        live = set(TaskState) - TERMINAL_STATES - INTERRUPTED_STATES
        assert live == {TaskState.UNSPECIFIED, TaskState.SUBMITTED, TaskState.WORKING}

    def test_enum_values_are_protojson_names(self) -> None:
        """§5.5: enums serialise as the proto's own SCREAMING_SNAKE names."""
        assert TaskState.INPUT_REQUIRED.value == "TASK_STATE_INPUT_REQUIRED"
        assert Role.USER.value == "ROLE_USER"


class TestWireShape:
    def test_fields_serialise_as_camel_case(self) -> None:
        """§5.5: "All JSON serializations ... MUST use camelCase"."""
        message = Message(
            message_id="m1", context_id="c1", role=Role.USER, parts=(Part.from_text("hi"),)
        )
        assert wire_dict(message) == {
            "messageId": "m1",
            "contextId": "c1",
            "role": "ROLE_USER",
            "parts": [{"text": "hi"}],
        }

    def test_unset_absent_fields_are_omitted(self) -> None:
        """§5.7: an optional field that was never set is not `null` on the
        wire, and an empty repeated field is a proto default."""
        task = Task(id="t1", status=TaskStatus(state=TaskState.WORKING))
        assert wire_dict(task) == {"id": "t1", "status": {"state": "TASK_STATE_WORKING"}}

    def test_an_explicitly_set_default_survives(self) -> None:
        """§8.4.1: "Optional fields explicitly set to defaults ... MUST be
        included", which is what makes a signature reproducible."""
        task = Task(id="t1", context_id=None, status=TaskStatus(state=TaskState.WORKING))
        assert "contextId" in wire_dict(task)

    def test_a_part_must_carry_exactly_one_kind_of_content(self) -> None:
        with pytest.raises(ValueError, match="oneof"):
            Part()
        with pytest.raises(ValueError, match="oneof"):
            Part(text="hi", url="https://example.test/x")

    def test_unknown_fields_are_ignored_not_refused(self) -> None:
        """§5.7: "Implementations SHOULD ignore unrecognized fields", so a peer
        speaking 1.1 is not a parse failure."""
        task = Task.model_validate(
            {"id": "t1", "status": {"state": "TASK_STATE_WORKING"}, "somethingNew": 42}
        )
        assert task.id == "t1"

    def test_timestamps_are_utc_with_a_z_suffix(self) -> None:
        """§5.6.1: ISO 8601, millisecond precision, never an offset."""
        from datetime import UTC, datetime

        status = TaskStatus(
            state=TaskState.WORKING,
            timestamp=datetime(2026, 4, 1, 12, 30, 5, 250000, tzinfo=UTC),
        )
        assert wire_dict(status)["timestamp"] == "2026-04-01T12:30:05.250Z"


def _envelope(**kwargs: object) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": 1, **kwargs}


class TestJsonRpcBinding:
    def test_a_valid_request_parses_to_its_message(self) -> None:
        method, params, request_id = parse_request(
            _envelope(
                method="SendMessage",
                params={
                    "message": {
                        "messageId": "m1",
                        "role": "ROLE_USER",
                        "parts": [{"text": "hello"}],
                    }
                },
            )
        )
        assert method is A2AMethod.SEND_MESSAGE
        assert isinstance(params, SendMessageRequest)
        assert params.message.text == "hello"
        assert request_id == 1

    def test_missing_params_is_an_empty_object(self) -> None:
        """§9.4.8's own GetExtendedAgentCard example omits `params`."""
        method, _, _ = parse_request(_envelope(method="GetExtendedAgentCard"))
        assert method is A2AMethod.GET_EXTENDED_AGENT_CARD

    def test_a_wrong_jsonrpc_version_is_an_invalid_request(self) -> None:
        with pytest.raises(InvalidRequestError) as err:
            parse_request({"jsonrpc": "1.0", "id": 1, "method": "GetTask"})
        assert err.value.jsonrpc_code == -32600

    def test_positional_params_are_refused(self) -> None:
        """A2A defines no method taking positional parameters, so guessing at
        an array would be inventing a binding."""
        with pytest.raises(InvalidRequestError):
            parse_request(_envelope(method="GetTask", params=["t1"]))

    def test_an_unknown_method_names_what_is_implemented(self) -> None:
        with pytest.raises(MethodNotFoundError) as err:
            parse_request(_envelope(method="message/send", params={}))
        assert err.value.jsonrpc_code == -32601
        assert "SendMessage" in str(err.value)

    def test_bad_parameters_report_why(self) -> None:
        with pytest.raises(InvalidParamsError) as err:
            parse_request(_envelope(method="GetTask", params={"id": ""}))
        assert err.value.jsonrpc_code == -32602
        assert "GetTask" in str(err.value)

    def test_a_body_that_is_not_json_is_minus_32700(self) -> None:
        with pytest.raises(JsonParseError) as err:
            parse_body("{not json", json.loads)
        assert err.value.jsonrpc_code == -32700

    def test_streaming_methods_are_the_two_that_answer_with_sse(self) -> None:
        assert {
            A2AMethod.SEND_STREAMING_MESSAGE,
            A2AMethod.SUBSCRIBE_TO_TASK,
        } == STREAMING_METHODS

    def test_a_success_envelope_carries_the_id_and_a_camel_case_result(self) -> None:
        task = Task(id="t1", context_id="c1", status=TaskStatus(state=TaskState.COMPLETED))
        assert success_response("abc", task) == {
            "jsonrpc": "2.0",
            "id": "abc",
            "result": {
                "id": "t1",
                "contextId": "c1",
                "status": {"state": "TASK_STATE_COMPLETED"},
            },
        }

    def test_an_error_envelope_carries_the_typed_code_and_error_info(self) -> None:
        """§9.5: `data` is an array of objects each keyed by `@type`."""
        response = error_response(7, TaskNotFoundError("no such task", metadata={"taskId": "t1"}))
        assert response["error"]["code"] == -32001
        detail = response["error"]["data"][0]
        assert detail["@type"] == "type.googleapis.com/google.rpc.ErrorInfo"
        assert detail["reason"] == "TASK_NOT_FOUND"
        assert detail["domain"] == "a2a-protocol.org"
        assert detail["metadata"] == {"taskId": "t1"}

    def test_a_stream_frame_repeats_the_request_id(self) -> None:
        """§9.4.2's frames each carry the id, which is what lets a client
        demultiplex two streams on one connection."""
        event = StreamResponse(task=Task(id="t1", status=TaskStatus(state=TaskState.WORKING)))
        frame = stream_frame("abc", event)
        assert frame["id"] == "abc"
        assert frame["result"]["task"]["id"] == "t1"


class TestErrorMappingTable:
    """§5.4, checked as a table rather than case by case."""

    @pytest.mark.parametrize(
        ("error_type", "code", "status", "grpc"),
        [
            ("TaskNotFoundError", -32001, 404, "NOT_FOUND"),
            ("TaskNotCancelableError", -32002, 400, "FAILED_PRECONDITION"),
            ("PushNotificationNotSupportedError", -32003, 400, "FAILED_PRECONDITION"),
            ("UnsupportedOperationError", -32004, 400, "FAILED_PRECONDITION"),
            ("ContentTypeNotSupportedError", -32005, 400, "INVALID_ARGUMENT"),
            ("InvalidAgentResponseError", -32006, 500, "INTERNAL"),
            ("ExtendedAgentCardNotConfiguredError", -32007, 400, "FAILED_PRECONDITION"),
            ("ExtensionSupportRequiredError", -32008, 400, "FAILED_PRECONDITION"),
            ("VersionNotSupportedError", -32009, 400, "FAILED_PRECONDITION"),
        ],
    )
    def test_each_row(self, error_type: str, code: int, status: int, grpc: str) -> None:
        from psych_runtime.a2a import errors

        kind: type[A2AError] = getattr(errors, error_type)
        assert kind.jsonrpc_code == code
        assert kind.http_status == status
        assert kind.grpc_status == grpc

    def test_codes_are_unique(self) -> None:
        codes = [kind.jsonrpc_code for kind in A2AError.__subclasses__()]
        assert len(codes) == len(set(codes))

    def test_the_same_failure_renders_consistently_in_both_bindings(self) -> None:
        """§5.1: an agent offering two bindings must handle errors the same
        way in both."""
        error = TaskNotFoundError("gone")
        status, body = error_body(error)
        rendered = body["error"]
        assert isinstance(rendered, dict)
        assert status == 404
        assert rendered["status"] == "NOT_FOUND"
        assert rendered["details"][0]["reason"] == jsonrpc_error_object(error)["data"][0]["reason"]

    def test_the_rest_body_is_a_google_rpc_status(self) -> None:
        """§11.6: `code` is the HTTP status, beside a `status` string."""
        rendered = rpc_status_body(TaskNotFoundError("gone"))["error"]
        assert isinstance(rendered, dict)
        assert rendered["code"] == 404
        assert rendered["message"] == "gone"


class TestRestBinding:
    def test_routes_match_the_specification(self) -> None:
        assert REST_ROUTES[A2AMethod.SEND_MESSAGE] == ("POST", "/message:send")
        assert REST_ROUTES[A2AMethod.SEND_STREAMING_MESSAGE] == ("POST", "/message:stream")
        assert REST_ROUTES[A2AMethod.GET_TASK] == ("GET", "/tasks/{id}")
        assert REST_ROUTES[A2AMethod.CANCEL_TASK] == ("POST", "/tasks/{id}:cancel")
        assert REST_ROUTES[A2AMethod.DELETE_TASK_PUSH_NOTIFICATION_CONFIG] == (
            "DELETE",
            "/tasks/{task_id}/pushNotificationConfigs/{id}",
        )

    def test_subscribe_follows_the_proto_not_the_prose(self) -> None:
        """The proto annotates `get:` and §11.3.2 writes POST. §1.4 makes the
        proto normative, so the table says GET; the server accepts both."""
        assert REST_ROUTES[A2AMethod.SUBSCRIBE_TO_TASK] == ("GET", "/tasks/{id}:subscribe")

    def test_query_parameters_are_camel_case(self) -> None:
        """§11.5's naming table."""
        request = list_tasks_request_from_query(
            {
                "contextId": "c1",
                "status": "TASK_STATE_WORKING",
                "pageSize": "25",
                "includeArtifacts": "true",
            },
            tenant="agent-7",
        )
        assert request.context_id == "c1"
        assert request.status is TaskState.WORKING
        assert request.page_size == 25
        assert request.include_artifacts is True
        assert request.tenant == "agent-7"

    def test_absent_query_parameters_stay_absent(self) -> None:
        request = list_tasks_request_from_query({})
        assert "context_id" not in request.model_fields_set

    def test_a_page_size_beyond_the_maximum_is_refused(self) -> None:
        """The proto: "The maximum value is 100"."""
        with pytest.raises(InvalidParamsError):
            list_tasks_request_from_query({"pageSize": "500"})

    def test_history_length_comes_off_the_query_string(self) -> None:
        request = get_task_request_from_query("t1", {"historyLength": "10"})
        assert request.id == "t1"
        assert request.history_length == 10

    def test_an_sse_frame_is_one_data_line(self) -> None:
        """A payload split across data lines is a payload a lenient client can
        reassemble wrongly."""
        frame = sse_frame({"a": 1, "b": "two"})
        assert frame == b'data: {"a":1,"b":"two"}\n\n'
        assert frame.count(b"\ndata:") == 0


class TestVersionNegotiation:
    def test_the_declared_version(self) -> None:
        assert PROTOCOL_VERSION == "1.0"

    def test_a_matching_version_is_accepted(self) -> None:
        assert negotiate_version("1.0") == "1.0"

    def test_a_patch_number_is_ignored(self) -> None:
        """§3.6: patch numbers "MUST not be considered" in negotiation."""
        assert negotiate_version("1.0.7") == "1.0"

    def test_an_absent_header_means_0_3_and_is_refused(self) -> None:
        """§3.6.2: "Agents MUST interpret empty value as 0.3 version", and this
        implementation does not speak 0.3, so it says so rather than serving
        1.0 semantics to a 0.3 client."""
        assert ABSENT_VERSION == "0.3"
        with pytest.raises(VersionNotSupportedError) as err:
            negotiate_version(None)
        assert "0.3" in str(err.value)
        assert err.value.jsonrpc_code == -32009

    def test_whitespace_counts_as_absent(self) -> None:
        with pytest.raises(VersionNotSupportedError):
            negotiate_version("   ")

    def test_a_malformed_version_is_refused(self) -> None:
        with pytest.raises(VersionNotSupportedError):
            negotiate_version("latest")

    def test_an_interface_may_declare_its_own_supported_set(self) -> None:
        """§3.6.2: an agent "CAN expose multiple interfaces for the same
        transport with different versions"."""
        assert negotiate_version("0.3", supported=frozenset({"0.3", "1.0"})) == "0.3"


class TestExtensions:
    def test_the_header_is_comma_separated(self) -> None:
        assert parse_extensions_header("https://a/v1, https://b/v1") == (
            "https://a/v1",
            "https://b/v1",
        )

    def test_duplicates_collapse_and_order_is_kept(self) -> None:
        assert parse_extensions_header("b, a, b") == ("b", "a")

    def test_no_header_is_no_extensions(self) -> None:
        assert parse_extensions_header(None) == ()

    def test_only_declared_extensions_activate(self) -> None:
        """§4.6.3: an extension the agent does not support is ignored for the
        interaction, never approximated with an older version."""
        declared = (AgentExtension(uri="https://a/v1"),)
        assert activated_extensions(declared, ["https://a/v1", "https://z/v9"]) == ("https://a/v1",)

    def test_a_required_extension_must_be_opted_into(self) -> None:
        declared = (
            AgentExtension(uri="https://a/v1", required=True),
            AgentExtension(uri="https://b/v1"),
        )
        with pytest.raises(ExtensionSupportRequiredError) as err:
            require_declared_extensions(declared, [])
        assert err.value.jsonrpc_code == -32008
        assert "https://a/v1" in str(err.value)

    def test_opting_in_satisfies_the_requirement(self) -> None:
        declared = (AgentExtension(uri="https://a/v1", required=True),)
        assert require_declared_extensions(declared, ["https://a/v1"]) == ("https://a/v1",)
