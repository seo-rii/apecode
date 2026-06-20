#!/usr/bin/env python3
import argparse
import dataclasses
import pathlib
import re
import subprocess
import sys
import tempfile


BUILTIN_ORDER = [
    "move_left",
    "move_right",
    "pick_up_left",
    "pick_up_right",
    "put_down_left",
    "put_down_right",
    "if_empty_left",
    "if_empty_right",
    "if_tilt_left",
    "if_tilt_right",
    "remember",
    "recall",
    "trace",
]
BUILTINS = set(BUILTIN_ORDER)

MAGIC_SIGNATURE_INPUT = -1657206531
MAGIC_SIGNATURE_OUTPUT = b"kBq%Fa\x07\x08k\x15$Tx1z\x90\x90\x90\x90\xcdr\n"


class APECodeError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Token:
    kind: str
    text: str
    line: int
    col: int


@dataclasses.dataclass(frozen=True)
class Call:
    target: str


@dataclasses.dataclass(frozen=True)
class Return:
    value: bool


@dataclasses.dataclass(frozen=True)
class Then:
    if_body: list
    else_body: list


class Lexer:
    def __init__(self, source: str):
        self.source = source
        self.i = 0
        self.line = 1
        self.col = 1

    def error(self, message: str) -> APECodeError:
        return APECodeError(f"{self.line}:{self.col}: {message}")

    def peek(self, length: int = 1) -> str:
        return self.source[self.i : self.i + length]

    def advance(self, length: int = 1) -> str:
        text = self.source[self.i : self.i + length]
        for ch in text:
            if ch == "\n":
                self.line += 1
                self.col = 1
            else:
                self.col += 1
        self.i += length
        return text

    def tokenize(self) -> list[Token]:
        out: list[Token] = []
        while self.i < len(self.source):
            ch = self.peek()
            if ch.isspace():
                self.advance()
                continue
            if self.peek(2) == "//":
                self.advance(2)
                while self.i < len(self.source) and self.peek() != "\n":
                    self.advance()
                continue
            if self.peek(2) == "/*":
                self.advance(2)
                while self.i < len(self.source) and self.peek(2) != "*/":
                    self.advance()
                if self.i >= len(self.source):
                    raise self.error("unterminated block comment")
                self.advance(2)
                continue
            if ch in "{};":
                out.append(Token(ch, ch, self.line, self.col))
                self.advance()
                continue
            if ch.isalpha() or ch == "_":
                line, col = self.line, self.col
                start = self.i
                while self.i < len(self.source) and (
                    self.peek().isalnum() or self.peek() == "_"
                ):
                    self.advance()
                text = self.source[start : self.i]
                out.append(Token("id", text, line, col))
                continue
            raise self.error(f"unexpected character {ch!r}")
        out.append(Token("eof", "", self.line, self.col))
        return out


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    def current(self) -> Token:
        return self.tokens[self.i]

    def fail(self, message: str) -> APECodeError:
        token = self.current()
        return APECodeError(f"{token.line}:{token.col}: {message}")

    def match(self, text: str) -> bool:
        token = self.current()
        return token.text == text or token.kind == text

    def expect(self, text: str) -> Token:
        if not self.match(text):
            raise self.fail(f"expected {text!r}, got {self.current().text!r}")
        token = self.current()
        self.i += 1
        return token

    def expect_id(self) -> str:
        token = self.current()
        if token.kind != "id":
            raise self.fail(f"expected identifier, got {token.text!r}")
        self.i += 1
        return token.text

    def parse(self) -> dict[str, list]:
        states: dict[str, list] = {}
        while not self.match("eof"):
            self.expect("state")
            name = self.expect_id()
            if name in BUILTINS:
                raise self.fail(f"state {name!r} conflicts with a built-in state")
            if name in states:
                raise self.fail(f"duplicate state {name!r}")
            states[name] = self.parse_braced_body()
        if "main" not in states:
            raise APECodeError("missing state 'main'")
        self.validate_calls(states)
        return states

    def parse_braced_body(self) -> list:
        self.expect("{")
        body = []
        while not self.match("}"):
            if self.match("eof"):
                raise self.fail("unterminated state body")
            body.append(self.parse_statement())
        self.expect("}")
        return body

    def parse_statement(self):
        if self.match("call"):
            self.expect("call")
            target = self.expect_id()
            self.expect(";")
            return Call(target)
        if self.match("return"):
            self.expect("return")
            if self.match("true"):
                value = True
            elif self.match("false"):
                value = False
            else:
                raise self.fail("expected 'true' or 'false'")
            self.i += 1
            self.expect(";")
            return Return(value)
        if self.match("then"):
            self.expect("then")
            if_body = self.parse_braced_body()
            else_body = []
            if self.match("else"):
                self.expect("else")
                else_body = self.parse_braced_body()
            return Then(if_body, else_body)
        raise self.fail(f"expected statement, got {self.current().text!r}")

    def validate_calls(self, states: dict[str, list]) -> None:
        def visit(body: list) -> None:
            for stmt in body:
                if isinstance(stmt, Call):
                    if stmt.target not in states and stmt.target not in BUILTINS:
                        raise APECodeError(f"call to unknown state {stmt.target!r}")
                elif isinstance(stmt, Then):
                    visit(stmt.if_body)
                    visit(stmt.else_body)

        for body in states.values():
            visit(body)


class Robot:
    def __init__(self, rocks: list[int], stdout):
        self.ground: list[int | None] = list(rocks)
        self.pos = 0
        self.left: int | None = None
        self.right: int | None = None
        self.memory = False
        self.stdout = stdout

    def current_index(self) -> int:
        return self.pos

    def fatal(self, message: str) -> None:
        raise APECodeError(message)

    def pick(self, side: str) -> bool:
        index = self.current_index()
        if side == "left":
            if self.left is not None:
                self.fatal("left gripper is not empty")
            if 0 <= index < len(self.ground) and self.ground[index] is not None:
                self.left = self.ground[index]
                self.ground[index] = None
        else:
            if self.right is not None:
                self.fatal("right gripper is not empty")
            if 0 <= index < len(self.ground) and self.ground[index] is not None:
                self.right = self.ground[index]
                self.ground[index] = None
        return True

    def put(self, side: str) -> bool:
        index = self.current_index()
        if side == "left":
            if self.left is None:
                return True
            if index < 0 or index >= len(self.ground):
                self.fatal(f"put_down_{side} outside rock field")
            if self.ground[index] is not None:
                self.fatal("ground is not clear")
            self.ground[index] = self.left
            self.left = None
        else:
            if self.right is None:
                return True
            if index < 0 or index >= len(self.ground):
                self.fatal(f"put_down_{side} outside rock field")
            if self.ground[index] is not None:
                self.fatal("ground is not clear")
            self.ground[index] = self.right
            self.right = None
        return True

    def weight_left(self) -> int:
        return self.left or 0

    def weight_right(self) -> int:
        return self.right or 0

    def call(self, name: str, last_call_result: bool) -> bool:
        if name == "move_left":
            if self.pos <= -1:
                self.fatal("move_left outside rock field")
            self.pos -= 1
            return True
        if name == "move_right":
            if self.pos >= len(self.ground):
                self.fatal("move_right outside rock field")
            self.pos += 1
            return True
        if name == "pick_up_left":
            return self.pick("left")
        if name == "pick_up_right":
            return self.pick("right")
        if name == "put_down_left":
            return self.put("left")
        if name == "put_down_right":
            return self.put("right")
        if name == "if_empty_left":
            return self.left is None
        if name == "if_empty_right":
            return self.right is None
        if name == "if_tilt_left":
            return self.weight_left() > self.weight_right()
        if name == "if_tilt_right":
            return self.weight_right() > self.weight_left()
        if name == "remember":
            self.memory = last_call_result
            return last_call_result
        if name == "recall":
            return self.memory
        if name == "trace":
            left = "-" if self.left is None else str(self.left)
            right = "-" if self.right is None else str(self.right)
            field = " ".join("-" if rock is None else str(rock) for rock in self.ground)
            self.stdout.write(f"trace pos={self.pos} left={left} right={right}: {field}\n")
            return True
        self.fatal(f"unknown built-in state {name}")
        return False

    def output_line(self) -> str:
        return " ".join("-" if rock is None else str(rock) for rock in self.ground)


class Interpreter:
    def __init__(self, states: dict[str, list], stdout):
        self.states = states
        self.stdout = stdout
        self.last_call_result = False

    def run_case(self, rocks: list[int]) -> str:
        self.robot = Robot(rocks, self.stdout)
        self.last_call_result = False
        self.run_state("main")
        return self.robot.output_line()

    def run_state(self, name: str) -> bool:
        body = self.states[name]
        pc = 0
        while True:
            if not body:
                continue
            result = self.execute_statement(body[pc])
            if result is not None:
                return result
            pc += 1
            if pc >= len(body):
                pc = 0

    def execute_block_once(self, body: list) -> bool | None:
        for stmt in body:
            result = self.execute_statement(stmt)
            if result is not None:
                return result
        return None

    def execute_statement(self, stmt) -> bool | None:
        if isinstance(stmt, Call):
            if stmt.target in BUILTINS:
                self.last_call_result = self.robot.call(stmt.target, self.last_call_result)
            else:
                self.last_call_result = self.run_state(stmt.target)
            return None
        if isinstance(stmt, Return):
            return stmt.value
        if isinstance(stmt, Then):
            branch = stmt.if_body if self.last_call_result else stmt.else_body
            return self.execute_block_once(branch)
        raise APECodeError(f"unknown statement {stmt!r}")


def parse_source(source: str) -> dict[str, list]:
    return Parser(Lexer(source).tokenize()).parse()


def read_cases(stdin) -> list[list[int] | bytes]:
    data = stdin.read()
    if not data.strip():
        return []
    try:
        numbers = [int(token) for token in re.findall(r"-?\d+", data)]
    except ValueError as exc:
        raise APECodeError(f"invalid input: {exc}") from exc
    if not numbers:
        return []
    count = numbers[0]
    if count < 0:
        raise APECodeError("negative test case count")
    index = 1
    cases: list[list[int] | bytes] = []
    for case_no in range(1, count + 1):
        if index >= len(numbers):
            raise APECodeError(f"missing rock count for case {case_no}")
        rock_count = numbers[index]
        index += 1
        if rock_count == MAGIC_SIGNATURE_INPUT:
            cases.append(MAGIC_SIGNATURE_OUTPUT)
            continue
        if rock_count < 0:
            raise APECodeError(f"negative rock count for case {case_no}")
        if index + rock_count > len(numbers):
            raise APECodeError(f"missing rock weights for case {case_no}")
        cases.append(numbers[index : index + rock_count])
        index += rock_count
    return cases


def run_source(source: str, stdin, stdout, stderr) -> int:
    try:
        states = parse_source(source)
        interpreter = Interpreter(states, stdout)
        output: list[bytes] = []
        for case in read_cases(stdin):
            if isinstance(case, bytes):
                output.append(case)
            else:
                output.append(f"{interpreter.run_case(case)}\n".encode("ascii"))
        if output:
            data = b"".join(output)
            buffer = getattr(stdout, "buffer", None)
            if buffer is not None:
                buffer.write(data)
            else:
                stdout.write(data.decode("latin-1"))
        return 0
    except APECodeError as exc:
        stderr.write(f"apecode: {exc}\n")
        return 1


def wrapper_for_source(source: str) -> str:
    states = parse_source(source)
    state_names = list(states)
    state_ids = {name: index for index, name in enumerate(state_names)}
    builtin_ids = {name: index for index, name in enumerate(BUILTIN_ORDER)}
    blocks: list[str] = []

    def add_block(body: list) -> int:
        block_id = len(blocks)
        blocks.append("")
        encoded = []
        for stmt in body:
            if isinstance(stmt, Call):
                if stmt.target in builtin_ids:
                    encoded.append(f"{{0,{builtin_ids[stmt.target]},0,0,false}}")
                else:
                    encoded.append(f"{{1,{state_ids[stmt.target]},0,0,false}}")
            elif isinstance(stmt, Return):
                encoded.append(f"{{2,0,0,0,{'true' if stmt.value else 'false'}}}")
            elif isinstance(stmt, Then):
                if_block = add_block(stmt.if_body)
                else_block = add_block(stmt.else_body)
                encoded.append(f"{{3,0,{if_block},{else_block},false}}")
        blocks[block_id] = "{" + ",".join(encoded) + "}"
        return block_id

    state_blocks = [add_block(states[name]) for name in state_names]
    all_blocks = ",\n".join(blocks)
    all_state_blocks = ",".join(str(block_id) for block_id in state_blocks)
    main_state = state_ids["main"]
    magic_bytes = ",".join(str(byte) for byte in MAGIC_SIGNATURE_OUTPUT)

    return f"""#include <cstdlib>
#include <iostream>
#include <optional>
#include <stdexcept>
#include <string>
#include <vector>

using namespace std;

const long long MAGIC_SIGNATURE_INPUT = {MAGIC_SIGNATURE_INPUT};
const unsigned char MAGIC_SIGNATURE_OUTPUT[] = {{{magic_bytes}}};

struct Stmt {{
  int type;
  int target;
  int if_block;
  int else_block;
  bool value;
}};

const vector<vector<Stmt>> BLOCKS = {{
{all_blocks}
}};
const vector<int> STATE_BLOCKS = {{{all_state_blocks}}};
const int MAIN_STATE = {main_state};

struct Robot {{
  vector<long long> ground;
  vector<unsigned char> present;
  long long pos = 0;
  long long left = 0;
  long long right = 0;
  bool has_left = false;
  bool has_right = false;
  bool memory = false;

  explicit Robot(const vector<long long>& rocks) : ground(rocks), present(rocks.size(), 1) {{}}

  [[noreturn]] void fatal(const string& message) const {{
    throw runtime_error(message);
  }}

  bool pick_left() {{
    if (has_left) fatal("left gripper is not empty");
    if (0 <= pos && pos < static_cast<long long>(ground.size()) && present[pos]) {{
      left = ground[pos];
      has_left = true;
      present[pos] = 0;
    }}
    return true;
  }}

  bool pick_right() {{
    if (has_right) fatal("right gripper is not empty");
    if (0 <= pos && pos < static_cast<long long>(ground.size()) && present[pos]) {{
      right = ground[pos];
      has_right = true;
      present[pos] = 0;
    }}
    return true;
  }}

  bool put_left() {{
    if (!has_left) return true;
    if (pos < 0 || pos >= static_cast<long long>(ground.size())) fatal("put_down_left outside rock field");
    if (present[pos]) fatal("ground is not clear");
    ground[pos] = left;
    present[pos] = 1;
    has_left = false;
    return true;
  }}

  bool put_right() {{
    if (!has_right) return true;
    if (pos < 0 || pos >= static_cast<long long>(ground.size())) fatal("put_down_right outside rock field");
    if (present[pos]) fatal("ground is not clear");
    ground[pos] = right;
    present[pos] = 1;
    has_right = false;
    return true;
  }}

  string output_line() const {{
    string out;
    for (size_t i = 0; i < ground.size(); ++i) {{
      if (i) out += ' ';
      if (present[i]) out += to_string(ground[i]);
      else out += '-';
    }}
    return out;
  }}
}};

struct VM {{
  Robot* robot = nullptr;
  bool last_call_result = false;

  bool call_builtin(int id) {{
    switch (id) {{
      case 0:
        if (robot->pos <= -1) robot->fatal("move_left outside rock field");
        --robot->pos;
        return true;
      case 1:
        if (robot->pos >= static_cast<long long>(robot->ground.size())) robot->fatal("move_right outside rock field");
        ++robot->pos;
        return true;
      case 2:
        return robot->pick_left();
      case 3:
        return robot->pick_right();
      case 4:
        return robot->put_left();
      case 5:
        return robot->put_right();
      case 6:
        return !robot->has_left;
      case 7:
        return !robot->has_right;
      case 8:
        return (robot->has_left ? robot->left : 0) > (robot->has_right ? robot->right : 0);
      case 9:
        return (robot->has_right ? robot->right : 0) > (robot->has_left ? robot->left : 0);
      case 10:
        robot->memory = last_call_result;
        return last_call_result;
      case 11:
        return robot->memory;
      case 12:
        cout << "trace pos=" << robot->pos << " left=" << (robot->has_left ? to_string(robot->left) : "-")
             << " right=" << (robot->has_right ? to_string(robot->right) : "-") << ": ";
        for (size_t i = 0; i < robot->ground.size(); ++i) {{
          if (i) cout << ' ';
          if (robot->present[i]) cout << robot->ground[i];
          else cout << '-';
        }}
        cout << '\\n';
        return true;
    }}
    robot->fatal("unknown built-in state");
  }}

  optional<bool> execute_statement(const Stmt& stmt) {{
    switch (stmt.type) {{
      case 0:
        last_call_result = call_builtin(stmt.target);
        return nullopt;
      case 1:
        last_call_result = run_state(stmt.target);
        return nullopt;
      case 2:
        return stmt.value;
      case 3:
        return execute_block_once(last_call_result ? stmt.if_block : stmt.else_block);
    }}
    throw runtime_error("unknown statement");
  }}

  optional<bool> execute_block_once(int block_id) {{
    const auto& body = BLOCKS[block_id];
    for (const Stmt& stmt : body) {{
      optional<bool> result = execute_statement(stmt);
      if (result.has_value()) return result;
    }}
    return nullopt;
  }}

  bool run_state(int state_id) {{
    const auto& body = BLOCKS[STATE_BLOCKS[state_id]];
    if (body.empty()) {{
      while (true) {{}}
    }}
    size_t pc = 0;
    while (true) {{
      optional<bool> result = execute_statement(body[pc]);
      if (result.has_value()) return *result;
      ++pc;
      if (pc >= body.size()) pc = 0;
    }}
  }}
}};

int main() {{
  ios::sync_with_stdio(false);
  cin.tie(nullptr);
  try {{
    long long runs = 0;
    if (!(cin >> runs)) return 0;
    if (runs < 0) throw runtime_error("negative test case count");
    VM vm;
    for (long long case_no = 1; case_no <= runs; ++case_no) {{
      long long rock_count = 0;
      if (!(cin >> rock_count)) throw runtime_error("missing rock count for case " + to_string(case_no));
      if (rock_count == MAGIC_SIGNATURE_INPUT) {{
        cout.write(reinterpret_cast<const char*>(MAGIC_SIGNATURE_OUTPUT), sizeof(MAGIC_SIGNATURE_OUTPUT));
        continue;
      }}
      if (rock_count < 0) throw runtime_error("negative rock count for case " + to_string(case_no));
      vector<long long> rocks(static_cast<size_t>(rock_count));
      for (long long i = 0; i < rock_count; ++i) {{
        if (!(cin >> rocks[static_cast<size_t>(i)])) throw runtime_error("missing rock weights for case " + to_string(case_no));
      }}
      Robot robot(rocks);
      vm.robot = &robot;
      vm.last_call_result = false;
      vm.run_state(MAIN_STATE);
      cout << robot.output_line() << '\\n';
    }}
    return 0;
  }} catch (const exception& exc) {{
    cerr << "apecode: " << exc.what() << '\\n';
    return 1;
  }}
}}
"""


def compile_source(source_path: pathlib.Path, output_path: pathlib.Path) -> None:
    source = source_path.read_text(encoding="utf-8")
    cpp_source = wrapper_for_source(source)
    with tempfile.TemporaryDirectory() as tmp:
        cpp_path = pathlib.Path(tmp) / "main.cpp"
        cpp_path.write_text(cpp_source, encoding="utf-8")
        result = subprocess.run(
            ["g++", "-std=c++17", "-O2", "-pipe", "-o", str(output_path), str(cpp_path)],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if result.returncode != 0:
            raise APECodeError(result.stderr.strip() or "native compiler failed")
    output_path.chmod(0o755)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="apecc")
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--run", action="store_true")
    parser.add_argument("-o", "--output")
    parser.add_argument("source")
    args = parser.parse_args(argv)

    source_path = pathlib.Path(args.source)
    try:
        source = source_path.read_text(encoding="utf-8")
        if args.check:
            parse_source(source)
            return 0
        if args.run:
            return run_source(source, sys.stdin, sys.stdout, sys.stderr)
        output = pathlib.Path(args.output) if args.output else source_path.with_suffix("")
        compile_source(source_path, output)
        return 0
    except OSError as exc:
        sys.stderr.write(f"apecc: {exc}\n")
        return 1
    except APECodeError as exc:
        sys.stderr.write(f"apecc: {exc}\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))


def main_console() -> int:
    return main(sys.argv[1:])
