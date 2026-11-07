import io
import json
import subprocess
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError

import local_model_service as local
from model_settings import DEFAULTS


class LocalModelServiceTest(unittest.TestCase):
    def setUp(self):
        self.profile=DEFAULTS["lmstudio"]
        opener=Mock()
        self.open=opener.open
        self.enterContext(patch.object(local,"build_opener",return_value=opener))

    def models(self,ids):
        return io.BytesIO(json.dumps({"object":"list","data":[{"id":s} for s in ids]}).encode())

    def test_ready_missing_and_auth_do_not_start_a_service(self):
        self.open.side_effect=[self.models([self.profile.model]),self.models([]),
            HTTPError(self.profile.endpoint,401,"denied",None,io.BytesIO())]
        with patch.object(local,"_start") as start:
            for expected in ("ready","model_missing","error"):
                state=local.prepare_local_service(self.profile,key="fixture-secret")
                self.assertEqual(state["status"],expected)
                self.assertNotIn("fixture-secret",str(state))
            start.assert_not_called()
        self.assertEqual(self.open.call_args.args[0].get_header("Authorization"),"Bearer fixture-secret")

    def test_only_refused_loopback_starts_with_configured_port_and_hidden_command(self):
        self.open.side_effect=[URLError(ConnectionRefusedError()),self.models([self.profile.model])]
        with patch.object(local,"_executable",return_value="lms.exe"), patch.object(local.subprocess,"run",
                return_value=subprocess.CompletedProcess([],0)) as launch:
            answer=local.prepare_local_service(replace(self.profile,endpoint="http://localhost:5678/v1"))
        self.assertTrue(answer["ready"])
        self.assertEqual([c.args[0] for c in launch.call_args_list],[
            ["lms.exe","daemon","up","--json"],["lms.exe","server","start","--bind","127.0.0.1","--port","5678"]])
        self.assertEqual(launch.call_args.kwargs["creationflags"],getattr(subprocess,"CREATE_NO_WINDOW",0))
        self.assertEqual(launch.call_args.kwargs["stdin"],subprocess.DEVNULL)

    def test_remote_timeout_and_http_error_leave_other_servers_alone(self):
        self.open.side_effect=URLError(TimeoutError())
        with patch.object(local,"_start") as start:
            self.assertEqual(local.prepare_local_service(DEFAULTS["compatible"])["status"],"unmanaged")
            for endpoint in ("http://192.168.1.7:1234/v1","http://something.localhost:1234/v1"):
                state=local.prepare_local_service(replace(self.profile,endpoint=endpoint))
                self.assertTrue(state["ready"])
                self.assertEqual(state["status"],"unmanaged")
            self.open.assert_not_called()
            self.assertEqual(local.prepare_local_service(self.profile)["status"],"waiting")
            start.assert_not_called()

    def test_ollama_bound_and_model_failure_retry_next_cycle(self):
        self.open.side_effect=[URLError(ConnectionRefusedError()),self.models([])]
        profile=replace(DEFAULTS["ollama"],endpoint="http://[::1]:11491/v1")
        with patch.object(local,"_executable",return_value="ollama.exe"),patch.object(local.subprocess,"Popen") as start:
            state=local.prepare_local_service(profile)
        self.assertFalse(state["ready"])
        self.assertEqual(state["status"],"model_missing")
        self.assertEqual(start.call_args.args[0],["ollama.exe","serve"])
        self.assertEqual(start.call_args.kwargs["env"]["OLLAMA_HOST"],"[::1]:11491")
        self.assertEqual(start.call_args.kwargs["env"]["OLLAMA_NO_CLOUD"],"1")

    def test_offline_with_missing_cli_or_no_ready_port_is_bounded(self):
        self.open.side_effect=URLError(ConnectionRefusedError())
        with patch.object(local,"_executable",return_value=None):
            self.assertIn("CLI",local.prepare_local_service(self.profile)["detail"])
        with patch.object(local,"_start",return_value=None),patch.object(local.time,"monotonic",side_effect=[0,19]):
            self.assertEqual(local.prepare_local_service(self.profile)["status"],"waiting")
