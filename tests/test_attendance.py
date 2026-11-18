import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from zju_cli.backend.modules.courses import AttendanceAdapter
from zju_cli.shared.config import Settings
from zju_cli.shared.errors import AuthenticationError, ConfigError, InputError, RemoteError, SessionExpired
from zju_cli.backend.command_registry import CommandRegistry
from zju_cli.backend.modules.schedules import SchedulesModule
from zju_cli.backend.modules.tasks import TasksModule
from zju_cli.backend.modules.autosign import NextRun
from zju_cli.shared.records import RecordStore as JobRecords
from zju_cli.backend.modules.autosign import AutosignModule, AttendanceService, AttendanceNotifications
from zju_cli.shared.attendance_state import offline_view
from zju_cli.shared.attendance_config import distance, estimate_position, validate_config


class AttendanceRuntime(CommandRegistry):
    def __init__(self, root):
        super().__init__()
        self.command_registry = self
        self.state_dir = Path(root)
        self.config = Settings("account", "test-password")
        self.tasks = TasksModule(self, self.config)
        self.schedules = SchedulesModule(self, self.config)
        self.service = None
        self.register("autosign.check", self.check, mode="job", durable=True,
                      resumable=True, internal_only=True)

    def check(self):
        result = self.service.check()
        self.schedules.set_next_delay("autosign", result.delay)
        return {"delay": result.delay}

    def close(self):
        self.schedules.close()
        self.tasks.close()


def activity(activity_id="1", **changes):
    return {"rollcall_id":activity_id,"course_title":"课程","title":"签到", "is_number":True,
            "is_radar":False,"is_expired":False,"rollcall_status":"in_progress","status":"absent", **changes}


class AutosignSessionTests(unittest.TestCase):
    def setUp(self):
        self.module = AutosignModule.__new__(AutosignModule)
        self.module.config = Settings("account", "password")
        self.module.daemon = Mock()
        self.module._session = None
        self.first, self.second = Mock(), Mock()
        self.module.daemon.zjuam.courses.side_effect = [self.first, self.second]
        self.adapter = Mock()
        self.adapter.activities.return_value = []
        self.module.daemon.courses.attendance_adapter.return_value = self.adapter

    def test_rounds_reuse_private_session(self):
        for _ in range(3):
            self.module._attendance_adapter().activities()
        self.module.daemon.zjuam.courses.assert_called_once_with(
            self.module.config.timeout_seconds)
        self.module.daemon.courses.attendance_adapter.assert_called_with(session=self.first)
        self.first.close.assert_not_called()

    def test_expiration_discards_session_and_discovery_refreshes_once(self):
        self.adapter.activities.side_effect = [SessionExpired("expired"), [], []]
        service = AttendanceService.__new__(AttendanceService)
        service.adapter_factory = self.module._attendance_adapter
        service._discover()
        service._discover()
        self.assertEqual(self.module.daemon.zjuam.courses.call_count, 2)
        self.first.close.assert_called_once_with()
        self.assertIs(self.module._session, self.second)

    def test_expired_write_is_not_replayed(self):
        self.adapter.number.side_effect = SessionExpired("expired")
        with self.assertRaises(SessionExpired):
            self.module._attendance_adapter().number("1", "1234")
        self.adapter.number.assert_called_once_with("1", "1234")
        self.assertIsNone(self.module._session)

    def test_close_releases_private_session(self):
        self.module._attendance_adapter()
        self.module.service = Mock()
        self.module.service.lock = threading.Lock()
        self.module.notifications = Mock()
        self.module.close()
        self.first.close.assert_called_once_with()
        self.assertIsNone(self.module._session)


class AttendanceTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.database=Path(self.temporary.name)/"scheduler.sqlite3"
        self.runtime=AttendanceRuntime(self.temporary.name)
        self.addCleanup(self.runtime.close)
        self.adapter=Mock()
        self.adapter.activities.return_value=[]
        self.adapter.code.return_value=None
        self.adapter.confirmed.return_value=False
        self.adapter.number.return_value={"confirmed":True}
        self.factory=Mock(return_value=self.adapter)
        self.notifications=Mock()
        self.service=AttendanceService(self.runtime,"account",self.factory,notifications=self.notifications)
        self.runtime.service=self.service
        self.service.restore()

    def state(self, key="1"):
        return self.service.records.get(self.service.namespace,key)

    def test_disabled_and_read_only_discovery_never_write(self):
        self.adapter.activities.return_value=[activity()]
        self.service.check()
        self.factory.assert_not_called()
        self.service.check(read_only=True)
        self.assertEqual(self.state()["state"],"discovered")
        self.adapter.number.assert_not_called()
        self.adapter.code.assert_not_called()
        self.assertFalse(self.service.status()["enabled"])

    def test_no_activities_completes_without_notifications(self):
        self.service.start()
        self.assertEqual(self.service.check(),NextRun(4))
        self.assertIsNotNone(self.service.status()["last_successful_check"])
        self.adapter.number.assert_not_called()
        self.notifications.publish.assert_called_with([])

    def test_history_counts_current_daemon_checks_including_failures(self):
        self.assertEqual(self.service.history()["query_count"],0)
        self.service.check()  # Disabled: no query performed.
        self.assertEqual(self.service.history()["query_count"],0)
        self.service.start();self.service.check()
        self.adapter.activities.side_effect=RemoteError("offline")
        self.service.check()
        self.assertEqual(self.service.history()["query_count"],2)
        self.assertEqual(self.service.history("2000-01-01")["query_count"],2)
        self.service.stop();self.service.start()
        self.assertEqual(self.service.history()["query_count"],2)
        restarted=AttendanceService(self.runtime,"account",self.factory)
        self.assertEqual(restarted.history()["query_count"],0)

    def test_platform_existing_expired_ended_and_unknown(self):
        self.adapter.activities.return_value=[activity("1",status="on_call"),
            activity("2",is_expired=True),activity("3",rollcall_status="ended"),
            activity("4",is_number=False)]
        self.service.start();self.service.check();self.service.check()
        self.assertEqual(self.state()["source"],"platform_existing")
        self.assertEqual(self.state("2")["state"],"ended")
        self.assertEqual(self.state("3")["state"],"ended")
        self.assertEqual(self.state("4")["reason"],"unknown_type")
        unknown=[n for c in self.notifications.publish.call_args_list for n in c.args[0] if "未知" in n["message"]]
        self.assertEqual(len(unknown),1)
        self.assertEqual(len(self.service.history()["events"]),3)
        self.adapter.number.assert_not_called()

    def test_available_code_one_write_and_persistent_dedup(self):
        self.adapter.activities.return_value=[activity()]
        self.adapter.code.return_value="0123"
        self.service.start();self.service.check();self.service.check()
        self.adapter.number.assert_called_once_with("1","0123")
        self.assertEqual(self.state()["source"],"tool")
        self.assertEqual(len(self.service.history()["events"]),1)
        self.assertNotIn("0123",json.dumps(self.state()))

    def test_missing_code_waits_for_explicit_answer_without_enumeration(self):
        self.adapter.activities.return_value=[activity()]
        self.service.start();self.service.check();self.service.check()
        self.assertEqual(self.state()["state"],"waiting_input")
        self.adapter.code.assert_called_once_with("1")
        self.adapter.number.assert_not_called()
        result=self.service.answer("1","9876")
        self.assertEqual(result["state"],"success")
        self.adapter.number.assert_called_once_with("1","9876")

    def test_rejected_code_waits_and_never_retries_automatic_code(self):
        self.adapter.activities.return_value=[activity()]
        self.adapter.code.return_value="1234"
        self.adapter.number.return_value={"confirmed":False,"rejected":True,"wrong_code":True}
        self.service.start();self.service.check();self.service.check()
        self.assertEqual(self.state()["state"],"waiting_input")
        self.assertEqual(self.adapter.number.call_count,1)
        self.service.answer("1","4321")
        self.assertEqual(self.adapter.number.call_count,2)
        self.assertEqual(self.state()["state"],"waiting_input")

    def test_lost_response_verifies_success_without_repeating_write(self):
        self.adapter.activities.return_value=[activity()]
        self.adapter.code.return_value="1234"
        self.adapter.number.side_effect=RemoteError("sensitive raw error")
        self.adapter.confirmed.return_value=True
        self.service.start();self.service.check();self.service.check()
        self.adapter.number.assert_called_once()
        self.adapter.confirmed.assert_called_once()
        self.assertEqual(self.state()["state"],"success")
        self.assertEqual(self.state()["source"],"tool")

    def test_unconfirmed_write_is_not_replayed_even_after_restart(self):
        self.adapter.activities.return_value=[activity()]
        self.adapter.code.return_value="1234"
        self.adapter.number.side_effect=RemoteError("lost")
        self.adapter.confirmed.side_effect=RemoteError("offline")
        self.service.start();self.service.check()
        self.assertEqual(self.state()["state"],"pending_verification")
        self.runtime.close()
        runtime=AttendanceRuntime(self.temporary.name)
        self.addCleanup(runtime.close)
        restored=AttendanceService(runtime,"account",self.factory)
        runtime.service=restored;restored.restore()
        restored.check()
        self.adapter.number.assert_called_once()
        self.assertEqual(restored.activities()["activities"][0]["state"],"pending_verification")

    def test_crash_after_intent_only_verifies(self):
        self.service.records.put(self.service.namespace,"1",{"activity_id":"1","course":"c","title":"t",
            "type":"number","state":"processing"})
        self.adapter.activities.return_value=[activity()]
        self.service.start();self.service.check()
        self.adapter.number.assert_not_called()
        self.assertEqual(self.state()["state"],"pending_verification")

    def test_backoff_and_recovery_use_unified_scheduler(self):
        self.adapter.activities.side_effect=RemoteError("token=must-not-appear")
        self.service.start()
        scheduler=self.runtime.schedules
        def run_round(delay):
            scheduler._rows["autosign"]["next_run_at"] = 0
            scheduler.tick()
            task_id = scheduler.get("autosign")["current_task_id"]
            self.assertIsNotNone(task_id)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if self.runtime.tasks.get(task_id)["state"] in {"succeeded", "failed"}:
                    break
                time.sleep(0.01)
            self.assertEqual(self.runtime.tasks.get(task_id)["state"], "succeeded")
            scheduler.tick()
            row = scheduler.get("autosign")
            self.assertEqual(row["last_result"]["delay"], delay)
            self.assertLessEqual(abs(row["next_run_at"] - time.time() - delay), 1)
        for delay in (4,8,16,32,60,60):
            run_round(delay)
        self.adapter.activities.side_effect=None
        run_round(4)
        self.assertEqual(self.service.status()["failures"],0)
        self.assertNotIn("must-not-appear",self.database.read_bytes().decode(errors="ignore"))

    def test_auth_failure_and_read_expiry(self):
        self.service.start()
        self.factory.side_effect=AuthenticationError("password")
        self.service.check()
        self.assertEqual(self.service.status()["error"],"authentication_failed")
        self.factory.side_effect=None
        self.adapter.activities.side_effect=[SessionExpired("expired"),[]]
        self.service.check()
        self.assertIsNone(self.service.status()["error"])

    def test_stop_between_radar_writes_and_after_inflight_confirmation(self):
        self.adapter.activities.return_value=[activity(is_number=False,is_radar=True)]
        self.service.start()
        def write(*args):
            self.service.stopping.set()
            return {"confirmed":True}
        self.adapter.radar.side_effect=write
        self.service.check()
        self.assertEqual(self.state()["state"],"success")
        self.adapter.radar.assert_called_once()
        self.service.stop();self.service.check()
        self.adapter.radar.assert_called_once()
        with self.assertRaises(InputError):self.service.answer("1","1234")

    def test_stop_orders_against_inflight_write_and_finishes_confirmation(self):
        entered=threading.Event();release=threading.Event()
        self.adapter.activities.return_value=[activity(),activity("2")]
        self.adapter.code.return_value="1234"
        def write(*args):
            entered.set()
            self.assertTrue(release.wait(3))
            raise RemoteError("response lost")
        self.adapter.number.side_effect=write
        self.adapter.confirmed.return_value=True
        self.service.start()
        thread=threading.Thread(target=self.service.check);thread.start()
        self.assertTrue(entered.wait(3))
        stopped=threading.Thread(target=self.service.stop);stopped.start()
        self.assertTrue(self.service.stopping.wait(3))
        release.set();thread.join(3);stopped.join(3)
        self.assertFalse(thread.is_alive());self.assertFalse(stopped.is_alive())
        self.assertEqual(self.state()["state"],"success")
        self.adapter.number.assert_called_once()
        self.assertFalse(self.service.status()["enabled"])

    def test_account_isolation_and_partial_config_preserves_radar(self):
        self.service.start({"radar":{"location":[120,30]},"desktop":False})
        self.service.start({"interval":8})
        self.assertEqual(self.service.config["radar"]["location"],[120,30])
        self.assertFalse(self.service.config["desktop"])
        self.service.records.put("autosign:other","1",{"state":"success"})
        self.assertEqual(self.service.activities()["activities"],[])

    def test_accepted_start_cannot_reopen_a_shutting_down_daemon(self):
        self.service.start();self.service.shutdown()
        with self.assertRaises(InputError):self.service.start()
        self.adapter.activities.return_value=[activity()]
        self.adapter.code.return_value="1234"
        self.service.check()
        self.adapter.number.assert_not_called()

    def test_radar_candidates_estimation_and_bounded_exhaustion(self):
        points=[[120,30],[120.01,30],[120,30.01]]
        target=[120.004,30.004]
        self.service.start({"radar":{"location":points[0],"candidates":points,"max_attempts":4}})
        self.adapter.activities.return_value=[activity(is_number=False,is_radar=True)]
        def radar(activity_id, point):
            if distance(point,target)<100:
                return {"confirmed":True}
            return {"confirmed":False,"rejected":True,"out_of_scope":True,"distance":distance(point,target)}
        self.adapter.radar.side_effect=radar
        self.service.check()
        self.assertEqual(self.adapter.radar.call_count,4)
        self.assertEqual(self.state()["state"],"success")
        self.assertLess(distance(self.adapter.radar.call_args.args[1],target),1)
        self.adapter.activities.return_value=[activity("2",is_number=False,is_radar=True)]
        self.adapter.radar.side_effect=None
        self.adapter.radar.return_value={"confirmed":False,"rejected":True,"out_of_scope":True,"distance":None}
        self.service.check();self.service.check()
        self.assertEqual(self.state("2")["reason"],"radar_attempts_exhausted")
        self.assertEqual(self.adapter.radar.call_count,7)

    def test_radar_rejection_and_unknown_response_do_not_repeat(self):
        self.service.start()
        self.adapter.activities.return_value=[activity(is_number=False,is_radar=True)]
        self.adapter.radar.return_value={"confirmed":False,"rejected":True,"out_of_scope":False}
        self.service.check();self.service.check()
        self.adapter.radar.assert_called_once()
        self.assertEqual(self.state()["reason"],"radar_rejected")
        self.adapter.activities.return_value=[activity("2",is_number=False,is_radar=True)]
        self.adapter.radar.return_value={"confirmed":False,"rejected":False}
        self.service.check();self.service.check()
        self.assertEqual(self.state("2")["state"],"pending_verification")
        self.assertEqual(self.adapter.radar.call_count,2)

    def test_restart_enabled_then_explicit_stop_stays_disabled(self):
        self.service.start();self.service.start()
        self.assertEqual(len(self.runtime.schedules.list()),1)
        self.service.shutdown();self.runtime.close()
        runtime=AttendanceRuntime(self.temporary.name);self.addCleanup(runtime.close)
        restored=AttendanceService(runtime,"account",self.factory)
        runtime.service=restored;restored.restore()
        self.assertTrue(restored.status()["enabled"])
        restored.stop();runtime.close()
        runtime2=AttendanceRuntime(self.temporary.name);self.addCleanup(runtime2.close)
        stopped=AttendanceService(runtime2,"account",self.factory)
        runtime2.service=stopped;stopped.restore()
        self.assertFalse(stopped.status()["enabled"])
        self.assertFalse(runtime2.schedules.get("autosign")["enabled"])

    def test_schedule_is_the_only_autosign_enabled_state(self):
        self.service.start()
        self.service.stop()
        control = self.service.records.get(self.service.namespace, "control")
        self.assertNotIn("enabled", control)
        self.assertFalse(self.service.status()["enabled"])

        self.runtime.schedules.enable("autosign")
        self.assertTrue(self.service.status()["enabled"])
        self.service.check()
        self.assertEqual(self.service.history()["query_count"], 1)

    def test_notification_failure_does_not_change_completed_attendance(self):
        self.notifications.publish.side_effect=RuntimeError("token=secret")
        self.adapter.activities.return_value=[activity()]
        self.adapter.code.return_value="1234"
        self.service.start();self.service.check();self.service.check()
        self.assertEqual(self.state()["state"],"success")
        self.adapter.number.assert_called_once()

    def test_offline_status_history_and_stop(self):
        self.service.start()
        self.adapter.activities.return_value=[activity(status="on_call_fine")]
        self.service.check();self.runtime.close()
        with patch("zju_cli.shared.runtime.runtime_dir",return_value=Path(self.temporary.name)), \
             patch("zju_cli.shared.storage.default_data_dir",return_value=Path(self.temporary.name)):
            self.assertTrue(offline_view("status")["enabled"])
            self.assertEqual(len(offline_view("history")["events"]),1)
            self.assertEqual(offline_view("history",{"date":"2000-01-01"}),{"query_count":0,"events":[]})
            self.assertFalse(offline_view("stop")["enabled"])


class AttendanceTransportTests(unittest.TestCase):
    def test_json_is_parsed_and_http200_error_is_not_success(self):
        session=Mock()
        session.request.return_value=(200,b'{"error_code":"wrong_number_code","number_code":"1234"}',"https://courses.zju.edu.cn/api/answer")
        adapter=AttendanceAdapter(session)
        result=adapter.number("1","1234")
        self.assertTrue(result["wrong_code"])
        self.assertFalse(result["confirmed"])
        self.assertNotIn("number_code",result)
        session.request.return_value=(200,b'{"status":"on_call"}',"https://courses.zju.edu.cn/api/answer")
        self.assertTrue(adapter.number("1","1234")["confirmed"])
        self.assertEqual(session.request.call_count,2)

    def test_code_requires_actual_usable_json_field(self):
        session=Mock();adapter=AttendanceAdapter(session)
        for value,expected in (({"number_code":"0123"},"0123"),({"number_code":1234},"1234"),
                               ({"number_code":123},None),({},None),({"number_code":None},None)):
            session.json.return_value=(200,value)
            self.assertEqual(adapter.code("1"),expected)

    def test_geometry_and_invalid_config(self):
        self.assertIsNone(estimate_position([([120,30],100)]*3))
        for value in ([],False,{"interval":float("nan")},{"radar":{"location":[999,30]}},
                      {"desktop":"yes"},{"radar":{"max_attempts":0}},
                      {"dingtalk":{"webhook":"http://example.test"}}):
            with self.assertRaises(ConfigError):validate_config(value)


class NotificationTests(unittest.TestCase):
    def test_export_repairs_unacknowledged_events_without_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            records=JobRecords(Path(temporary)/"scheduler.sqlite3")
            event={"event_id":"event-1","confirmed_at":"2026-10-23T12:00:00+08:00", "course":"c",
                "title":"t","activity_id":"1","type":"number","final_status":"success","source":"tool"}
            records.put("n","1",{"state":"success"},event=event)
            notifier=AttendanceNotifications(records,"n",Path(temporary),lambda:validate_config({"desktop":False}))
            try:
                with patch.object(records,"exported",side_effect=OSError("disk")):
                    with self.assertRaises(OSError):notifier.export()
                self.assertEqual(len(records.events("n",pending=True)),1)
                notifier.export();notifier.export()
                lines=(Path(temporary)/"autosign-2026-10-23.jsonl").read_text(encoding="utf-8").splitlines()
                self.assertEqual([json.loads(v)["event_id"] for v in lines],["event-1"])
                self.assertEqual(records.events("n",pending=True),[])
            finally:notifier.close()

    def test_delivery_failure_dedup_and_no_sensitive_exception_text(self):
        with tempfile.TemporaryDirectory() as temporary:
            records=JobRecords(Path(temporary)/"scheduler.sqlite3")
            desktop=Mock(side_effect=RuntimeError("private-webhook-token"))
            ding=Mock(side_effect=TimeoutError("private-webhook-token"))
            config=validate_config({"dingtalk":{"webhook":"https://oapi.dingtalk.com/robot/send?access_token=hidden"}})
            notifier=AttendanceNotifications(records,"n",Path(temporary),lambda:config,desktop=desktop,ding=ding)
            notice={"event_id":"one","message":"签到完成"}
            try:
                with patch("zju_cli.backend.modules.autosign.os.name","nt"):
                    notifier.publish([notice]);notifier.queue.join()
                    notifier.publish([notice]);notifier.queue.join()
                desktop.assert_called_once();ding.assert_called_once()
                self.assertEqual(records.get("n:notifications","one")["errors"],["RuntimeError","TimeoutError"])
                self.assertNotIn(b"private-webhook-token",records.database.read_bytes())
            finally:notifier.close()

    def test_non_windows_local_log_fallback_without_desktop(self):
        with tempfile.TemporaryDirectory() as temporary:
            records=JobRecords(Path(temporary)/"scheduler.sqlite3")
            desktop=Mock()
            notifier=AttendanceNotifications(records,"n",Path(temporary),lambda:validate_config(),desktop=desktop)
            try:
                with patch("zju_cli.backend.modules.autosign.os.name","posix"):
                    notifier.publish([{"event_id":"one","message":"需要输入"}]);notifier.queue.join()
                desktop.assert_not_called()
                self.assertTrue(records.get("n:notifications","one")["attempted"])
            finally:notifier.close()
