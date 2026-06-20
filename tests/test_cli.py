import io
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

from apecode.cli import APECodeError, compile_source, parse_source, run_source


IDENTITY = """state main {
  return true;
}
"""


PICK_AND_PUT = """state main {
  call if_empty_right;
  then { return false; }
  call pick_up_right;
  call put_down_right;
  return true;
}
"""


BUBBLE_PASS = """state false { return false; }
state true  { return true; }
state remember_false {
  call false;
  call remember;
  return false;
}
state remember_true {
  call true;
  call remember;
  return true;
}
state move_completely_left {
  call pick_up_left;
  call if_empty_left;
  then {
    call move_right;
    return true;
  }
  call put_down_left;
  call move_left;
}
state bubble_sort_pass {
  call pick_up_left;
  call move_right;
  call pick_up_right;
  call if_empty_left;
  then {
    call put_down_right;
    call move_left;
    call put_down_left;
    return true;
  }
  call if_empty_right;
  then {
    call put_down_right;
    call move_left;
    call put_down_left;
    return true;
  }
  call if_tilt_left;
  then {
    call remember_true;
    call put_down_left;
    call move_left;
    call put_down_right;
  } else {
    call put_down_right;
    call move_left;
    call put_down_left;
    call move_right;
  }
}
state main {
  call remember_false;
  call move_completely_left;
  call bubble_sort_pass;
  call recall;
  then {} else { return true; }
}
"""


class APECodeCLITest(unittest.TestCase):
    def test_run_source_outputs_rocks(self) -> None:
        stdout = io.StringIO()
        status = run_source(IDENTITY, io.StringIO("1\n3\n3 1 2\n"), stdout, io.StringIO())
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "3 1 2\n")

    def test_pick_and_put_builtin_states(self) -> None:
        stdout = io.StringIO()
        status = run_source(PICK_AND_PUT, io.StringIO("1\n1\n7\n"), stdout, io.StringIO())
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "7\n")

    def test_official_bubble_pass_sorts_sample(self) -> None:
        stdout = io.StringIO()
        status = run_source(
            BUBBLE_PASS,
            io.StringIO("1\n9\n7 1 6 3 4 9 2 5 8\n"),
            stdout,
            io.StringIO(),
        )
        self.assertEqual(status, 0)
        self.assertEqual(stdout.getvalue(), "1 2 3 4 5 6 7 8 9\n")

    def test_unknown_state_is_compile_error(self) -> None:
        with self.assertRaises(APECodeError):
            parse_source("state main { call missing; }\n")

    def test_compile_source_generates_executable_wrapper(self) -> None:
        root = pathlib.Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = pathlib.Path(tmp)
            source = tmp_path / "Main.ape"
            target = tmp_path / "Main"
            source.write_text(IDENTITY, encoding="utf-8")
            compile_source(source, target)

            env = dict(os.environ)
            env["PYTHONPATH"] = str(root / "src")
            result = subprocess.run(
                [str(target)],
                input="1\n2\n4 5\n",
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                check=False,
            )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "4 5\n")


if __name__ == "__main__":
    unittest.main()
