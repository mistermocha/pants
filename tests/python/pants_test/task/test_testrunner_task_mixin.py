# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import collections
import os
from unittest import TestCase

from mock import patch

from pants.base.exceptions import ErrorWhileTesting
from pants.task.task import TaskBase
from pants.task.testrunner_task_mixin import TestRunnerTaskMixin
from pants.util.contextutil import temporary_dir
from pants.util.dirutil import safe_open
from pants.util.process_handler import ProcessHandler
from pants.util.timeout import TimeoutReached
from pants.util.xml_parser import XmlParser
from pants_test.tasks.task_test_base import TaskTestBase


class DummyTestTarget(object):
  def __init__(self, name, timeout=None):
    self.name = name
    self.timeout = timeout
    self.address = collections.namedtuple('address', ['spec'])(name)

targetA = DummyTestTarget('TargetA')
targetB = DummyTestTarget('TargetB', timeout=1)
targetC = DummyTestTarget('TargetC', timeout=10)


class TestRunnerTaskMixinTest(TaskTestBase):
  @classmethod
  def task_type(cls):
    class TestRunnerTaskMixinTask(TestRunnerTaskMixin, TaskBase):
      call_list = []

      def _execute(self, all_targets):
        self.call_list.append(['_execute', all_targets])
        self._spawn_and_wait()

      def _spawn(self, *args, **kwargs):
        self.call_list.append(['_spawn', args, kwargs])

        class FakeProcessHandler(ProcessHandler):
          def wait(_):
            self.call_list.append(['process_handler.wait'])
            return 0

          def kill(_):
            self.call_list.append(['process_handler.kill'])

          def terminate(_):
            self.call_list.append(['process_handler.terminate'])

          def poll(_):
            self.call_list.append(['process_handler.poll'])

        return FakeProcessHandler()

      def _get_targets(self):
        return [targetA, targetB]

      def _test_target_filter(self):
        def target_filter(target):
          self.call_list.append(['target_filter', target])
          if target.name == 'TargetA':
            return False
          else:
            return True

        return target_filter

      def _validate_target(self, target):
        self.call_list.append(['_validate_target', target])

    return TestRunnerTaskMixinTask

  def test_execute_normal(self):
    task = self.create_task(self.context())

    task.execute()

    # Confirm that everything ran as expected
    self.assertIn(['target_filter', targetA], task.call_list)
    self.assertIn(['target_filter', targetB], task.call_list)
    self.assertIn(['_validate_target', targetB], task.call_list)
    self.assertIn(['_execute', [targetA, targetB]], task.call_list)

  def test_execute_skip(self):
    # Set the skip option
    self.set_options(skip=True)
    task = self.create_task(self.context())
    task.execute()

    # Ensure nothing got called
    self.assertListEqual(task.call_list, [])

  def test_get_timeouts_no_default(self):
    """If there is no default and one of the targets has no timeout, then there is no timeout for the entire run."""

    self.set_options(timeouts=True, timeout_default=None)
    task = self.create_task(self.context())

    self.assertIsNone(task._timeout_for_targets([targetA, targetB]))

  def test_get_timeouts_disabled(self):
    """If timeouts are disabled, there is no timeout for the entire run."""

    self.set_options(timeouts=False, timeout_default=2)
    task = self.create_task(self.context())

    self.assertIsNone(task._timeout_for_targets([targetA, targetB]))

  def test_get_timeouts_with_default(self):
    """If there is a default timeout, use that for targets which have no timeout set."""

    self.set_options(timeouts=True, timeout_default=2)
    task = self.create_task(self.context())

    self.assertEquals(task._timeout_for_targets([targetA, targetB]), 3)

  def test_get_timeouts_with_maximum(self):
    """If a timeout exceeds the maximum, set it to that."""

    self.set_options(timeouts=True, timeout_maximum=1)
    task = self.create_task(self.context())
    self.assertEquals(task._timeout_for_targets([targetC]), 1)

  def test_default_maximum_conflict(self):
    """If the default exceeds the maximum, throw an error."""

    self.set_options(timeouts=True, timeout_maximum=1, timeout_default=10)
    task = self.create_task(self.context())
    with self.assertRaises(ErrorWhileTesting):
      task.execute()


class TestRunnerTaskMixinSimpleTimeoutTest(TaskTestBase):
  @classmethod
  def task_type(cls):
    class TestRunnerTaskMixinTask(TestRunnerTaskMixin, TaskBase):
      call_list = []

      def _execute(self, all_targets):
        self.call_list.append(['_execute', all_targets])
        self._spawn_and_wait()

      def _spawn(self, *args, **kwargs):
        self.call_list.append(['_spawn', args, kwargs])

        class FakeProcessHandler(ProcessHandler):
          def wait(_):
            self.call_list.append(['process_handler.wait'])
            return 0

          def kill(_):
            self.call_list.append(['process_handler.kill'])

          def terminate(_):
            self.call_list.append(['process_handler.terminate'])

          def poll(_):
            self.call_list.append(['process_handler.poll'])
            return 0

        return FakeProcessHandler()

      def _get_targets(self):
        return [targetB]

      def _test_target_filter(self):
        def target_filter(target):
          return True

        return target_filter

      def _validate_target(self, target):
        self.call_list.append(['_validate_target', target])

    return TestRunnerTaskMixinTask

  def test_timeout(self):
    self.set_options(timeouts=True)
    task = self.create_task(self.context())

    with patch('pants.task.testrunner_task_mixin.Timeout') as mock_timeout:
      mock_timeout().__exit__.side_effect = TimeoutReached(1)

      with self.assertRaises(ErrorWhileTesting):
        task.execute()

      # Ensures that Timeout is instantiated with a 1 second timeout.
      args, kwargs = mock_timeout.call_args
      self.assertEqual(args, (1,))

  def test_timeout_disabled(self):
    self.set_options(timeouts=False)
    task = self.create_task(self.context())

    with patch('pants.task.testrunner_task_mixin.Timeout') as mock_timeout:
      task.execute()

      # Ensures that Timeout is instantiated with no timeout.
      args, kwargs = mock_timeout.call_args
      self.assertEqual(args, (None,))


class TestRunnerTaskMixinGracefulTimeoutTest(TaskTestBase):

  def create_process_handler(self, return_none_first=True):
    class FakeProcessHandler(ProcessHandler):
      call_list = []
      poll_called = False

      def wait(self):
        self.call_list.append(['process_handler.wait'])
        return 0

      def kill(self):
        self.call_list.append(['process_handler.kill'])

      def terminate(self):
        self.call_list.append(['process_handler.terminate'])

      def poll(self):
        print("poll called")
        self.call_list.append(['process_handler.poll'])
        if not self.poll_called and return_none_first:
          self.poll_called = True
          return None
        else:
          return 0

    return FakeProcessHandler()

  def task_type(cls):
    class TestRunnerTaskMixinTask(TestRunnerTaskMixin, TaskBase):
      call_list = []

      def _execute(self, all_targets):
        self.call_list.append(['_execute', all_targets])
        self._spawn_and_wait()

      def _spawn(self, *args, **kwargs):
        self.call_list.append(['_spawn', args, kwargs])

        return cls.process_handler

      def _get_targets(self):
        return [targetA, targetB]

      def _test_target_filter(self):
        def target_filter(target):
          self.call_list.append(['target_filter', target])
          if target.name == 'TargetA':
            return False
          else:
            return True

        return target_filter

      def _validate_target(self, target):
        self.call_list.append(['_validate_target', target])

    return TestRunnerTaskMixinTask

  def test_graceful_terminate_if_poll_is_none(self):
    self.process_handler = self.create_process_handler(return_none_first=True)

    self.set_options(timeouts=True)
    task = self.create_task(self.context())

    with patch('pants.task.testrunner_task_mixin.Timer') as mock_timer:
      def set_handler(dummy, handler):
        mock_timer_instance = mock_timer.return_value
        mock_timer_instance.start.side_effect = handler
        return mock_timer_instance

      mock_timer.side_effect = set_handler


      with self.assertRaises(ErrorWhileTesting):
        task.execute()

      # Ensure that all the calls we want to kill the process gracefully are made.
      self.assertEqual(self.process_handler.call_list,
                       [[u'process_handler.terminate'], [u'process_handler.poll'], [u'process_handler.kill'], [u'process_handler.wait']])

  def test_graceful_terminate_if_poll_is_zero(self):
    self.process_handler = self.create_process_handler(return_none_first=False)

    self.set_options(timeouts=True)
    task = self.create_task(self.context())

    with patch('pants.task.testrunner_task_mixin.Timer') as mock_timer:
      def set_handler(dummy, handler):
        mock_timer_instance = mock_timer.return_value
        mock_timer_instance.start.side_effect = handler
        return mock_timer_instance

      mock_timer.side_effect = set_handler


      with self.assertRaises(ErrorWhileTesting):
        task.execute()

      # Ensure that we only call terminate, and not kill.
      self.assertEqual(self.process_handler.call_list,
                       [[u'process_handler.terminate'], [u'process_handler.poll'], [u'process_handler.wait']])


class TestRunnerTaskMixinMultipleTargets(TaskTestBase):

  @classmethod
  def task_type(cls):
    class TestRunnerTaskMixinMultipleTargetsTask(TestRunnerTaskMixin, TaskBase):
      def _execute(self, all_targets):
        self._spawn_and_wait()

      def _spawn(self, *args, **kwargs):
        class FakeProcessHandler(ProcessHandler):
          def wait(self):
            return 0

          def kill(self):
            pass

          def terminate(self):
            pass

          def poll(self):
            pass

        return FakeProcessHandler()

      def _test_target_filter(self):
        return lambda target: True

      def _validate_target(self, target):
        pass

      def _get_targets(self):
        return [targetA, targetB]

      def _get_test_targets_for_spawn(self):
        return self.current_targets

    return TestRunnerTaskMixinMultipleTargetsTask

  def test_multiple_targets_single_target_timeout(self):
    with patch('pants.task.testrunner_task_mixin.Timeout') as mock_timeout:
      mock_timeout().__exit__.side_effect = TimeoutReached(1)

      self.set_options(timeouts=True)
      task = self.create_task(self.context())

      task.current_targets = [targetA]
      with self.assertRaises(ErrorWhileTesting) as cm:
        task.execute()
      self.assertEqual(len(cm.exception.failed_targets), 1)
      self.assertEqual(cm.exception.failed_targets[0].address.spec, 'TargetA')

      task.current_targets = [targetB]
      with self.assertRaises(ErrorWhileTesting) as cm:
        task.execute()
      self.assertEqual(len(cm.exception.failed_targets), 1)
      self.assertEqual(cm.exception.failed_targets[0].address.spec, 'TargetB')


class TestRunnerTaskMixinXmlParsing(TestRunnerTaskMixin, TestCase):
  @staticmethod
  def _raise_handler(e):
    raise e

  class CollectHandler(object):
    def __init__(self):
      self._errors = []

    def __call__(self, e):
      self._errors.append(e)

    @property
    def errors(self):
      return self._errors

  def test_parse_test_info_no_files(self):
    with temporary_dir() as xml_dir:
      test_info = self.parse_test_info(xml_dir, self._raise_handler)

      self.assertEqual({}, test_info)

  def test_parse_test_info_all_testcases(self):
    with temporary_dir() as xml_dir:
      with open(os.path.join(xml_dir, 'TEST-a.xml'), 'w') as fp:
        fp.write("""
        <testsuite failures="1" errors="1">
          <testcase classname="org.pantsbuild.Green" name="testOK" time="1.290"/>
          <testcase classname="org.pantsbuild.Failure" name="testFailure" time="0.27">
            <failure/>
          </testcase>
          <testcase classname="org.pantsbuild.Error" name="testError" time="0.932">
            <error/>
          </testcase>
          <testcase classname="org.pantsbuild.Skipped" name="testSkipped" time="0.1">
            <skipped/>
          </testcase>
        </testsuite>
        """)

      tests_info = self.parse_test_info(xml_dir, self._raise_handler)
      self.assertEqual(
        {
          'testOK': {
            'result_code': 'success',
            'time': 1.290
          },
          'testFailure': {
            'result_code': 'failure',
            'time': 0.27
          },
          'testError': {
            'result_code': 'error',
            'time': 0.932
          },
          'testSkipped': {
            'result_code': 'skipped',
            'time': 0.1
          }
        }, tests_info)

  def test_parse_test_info_with_missing_attributes(self):
    with temporary_dir() as xml_dir:
      with open(os.path.join(xml_dir, 'TEST-a.xml'), 'w') as fp:
        fp.write("""
        <testsuite failures="1">
          <testcase classname="org.pantsbuild.Green" name="testOK"/>
          <testcase classname="org.pantsbuild.Failure" time="0.27">
            <failure/>
          </testcase>
          <testcase classname="org.pantsbuild.Skipped" name="testSkipped" time="0.1" extra="">
            <skipped/>
          </testcase>
        </testsuite>
        """)

      tests_info = self.parse_test_info(xml_dir, self._raise_handler)
      self.assertEqual(
        {
          'testOK': {
            'result_code': 'success',
            'time': None
          },
          '': {
            'result_code': 'failure',
            'time': 0.27
          },
          'testSkipped': {
            'result_code': 'skipped',
            'time': 0.1
          }
        }, tests_info)

  def test_parse_test_info_invalid_file_name(self):
    with temporary_dir() as xml_dir:
      with open(os.path.join(xml_dir, 'random.xml'), 'w') as fp:
        fp.write('<invalid></xml>')

      tests_info = self.parse_test_info(xml_dir, self._raise_handler)
      self.assertEqual({}, tests_info)

  def test_parse_test_info_invalid_dir(self):
    with temporary_dir() as xml_dir:
      with safe_open(os.path.join(xml_dir, 'subdir', 'TEST-c.xml'), 'w') as fp:
        fp.write('<invalid></xml>')

      tests_info = self.parse_test_info(xml_dir, self._raise_handler)
      self.assertEqual({}, tests_info)

  def test_parse_test_info_error_raise(self):
    with temporary_dir() as xml_dir:
      xml_file = os.path.join(xml_dir, 'TEST-bad.xml')
      with open(xml_file, 'w') as fp:
        fp.write('<invalid></xml>')
      with self.assertRaises(Exception) as exc:
        self.parse_test_info(xml_dir, self._raise_handler)
      self.assertEqual(xml_file, exc.exception.xml_path)
      self.assertIsInstance(exc.exception.cause, XmlParser.XmlError)

  def test_parse_test_info_error_continue(self):
    with temporary_dir() as xml_dir:
      bad_file1 = os.path.join(xml_dir, 'TEST-bad1.xml')
      with open(bad_file1, 'w') as fp:
        fp.write('<invalid></xml>')
      with open(os.path.join(xml_dir, 'TEST-good.xml'), 'w') as fp:
        fp.write("""
        <testsuite failures="0" errors="1">
          <testcase classname="org.pantsbuild.Error" name="testError" time="1.2">
            <error/>
          </testcase>
        </testsuite>
        """)

      collect_handler = self.CollectHandler()
      tests_info = self.parse_test_info(xml_dir, collect_handler)
      self.assertEqual(1, len(collect_handler.errors))
      self.assertEqual({bad_file1}, {e.xml_path for e in collect_handler.errors})

      self.assertEqual(
        {'testError':
          {
            'result_code': 'error',
            'time': 1.2
          }
        }, tests_info)

  def test_parse_test_info_extra_attributes(self):
    with temporary_dir() as xml_dir:
      with open(os.path.join(xml_dir, 'TEST-a.xml'), 'w') as fp:
        fp.write("""
        <testsuite errors="1">
          <testcase classname="org.pantsbuild.Green" name="testOK1" time="1.290" file="file.py"/>
          <testcase classname="org.pantsbuild.Green" name="testOK2" time="1.12"/>
          <testcase classname="org.pantsbuild.Green" name="testOK3" file="file.py"/>
          <testcase name="testOK4" time="1.79" file="file.py"/>
          <testcase name="testOK5" time="0.832"/>
          <testcase classname="org.pantsbuild.Error" name="testError" time="0.27" file="file.py">
            <error/>
          </testcase>
        </testsuite>
        """)

      tests_info = self.parse_test_info(xml_dir, self._raise_handler, ['file', 'classname'])
      self.assertEqual(
        {
          'testOK1': {
            'file': 'file.py',
            'classname': 'org.pantsbuild.Green',
            'result_code': 'success',
            'time': 1.290
          },
          'testOK2': {
            'file': '',
            'classname': 'org.pantsbuild.Green',
            'result_code': 'success',
            'time': 1.12
          },
          'testOK3': {
            'file': 'file.py',
            'classname': 'org.pantsbuild.Green',
            'result_code': 'success',
            'time': None
          },
          'testOK4': {
            'file': 'file.py',
            'classname': '',
            'result_code': 'success',
            'time': 1.79
          },
          'testOK5': {
            'file': '',
            'classname': '',
            'result_code': 'success',
            'time': 0.832
          },
          'testError': {
            'file': 'file.py',
            'classname': 'org.pantsbuild.Error',
            'result_code': 'error',
            'time': 0.27
          }
        }, tests_info)
