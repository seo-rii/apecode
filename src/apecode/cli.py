#!/usr/bin/env python3
import argparse
import base64
import dataclasses
import pathlib
import re
import sys


BUILTINS = {
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
}


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

    def left_index(self) -> int:
        return self.pos - 1

    def right_index(self) -> int:
        return self.pos

    def fatal(self, message: str) -> None:
        raise APECodeError(message)

    def empty_at(self, index: int) -> bool:
        return index < 0 or index >= len(self.ground) or self.ground[index] is None

    def pick(self, side: str) -> bool:
        index = self.left_index() if side == "left" else self.right_index()
        if index < 0 or index >= len(self.ground):
            self.fatal(f"pick_up_{side} outside rock field")
        if side == "left":
            if self.left is not None:
                self.fatal("left gripper is not empty")
            if self.ground[index] is None:
                self.fatal("no rock on the left")
            self.left = self.ground[index]
        else:
            if self.right is not None:
                self.fatal("right gripper is not empty")
            if self.ground[index] is None:
                self.fatal("no rock on the right")
            self.right = self.ground[index]
        self.ground[index] = None
        return True

    def put(self, side: str) -> bool:
        index = self.left_index() if side == "left" else self.right_index()
        if index < 0 or index >= len(self.ground):
            self.fatal(f"put_down_{side} outside rock field")
        if self.ground[index] is not None:
            self.fatal(f"ground on the {side} is not clear")
        if side == "left":
            if self.left is None:
                self.fatal("left gripper is empty")
            self.ground[index] = self.left
            self.left = None
        else:
            if self.right is None:
                self.fatal("right gripper is empty")
            self.ground[index] = self.right
            self.right = None
        return True

    def weight_left(self) -> int:
        return self.left or 0

    def weight_right(self) -> int:
        return self.right or 0

    def call(self, name: str, last_call_result: bool) -> bool:
        if name == "move_left":
            if self.pos <= 0:
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
            return self.empty_at(self.left_index())
        if name == "if_empty_right":
            return self.empty_at(self.right_index())
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


def read_cases(stdin) -> list[list[int]]:
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
    cases: list[list[int]] = []
    for case_no in range(1, count + 1):
        if index >= len(numbers):
            raise APECodeError(f"missing rock count for case {case_no}")
        rock_count = numbers[index]
        index += 1
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
        lines = [interpreter.run_case(case) for case in read_cases(stdin)]
        if lines:
            stdout.write("\n".join(lines))
            stdout.write("\n")
        return 0
    except APECodeError as exc:
        stderr.write(f"apecode: {exc}\n")
        return 1


def wrapper_for_source(source: str) -> str:
    encoded = base64.b64encode(source.encode("utf-8")).decode("ascii")
    return f"""#!/usr/bin/env python3
import base64
import sys

from apecode.cli import run_source

source = base64.b64decode({encoded!r}).decode("utf-8")
raise SystemExit(run_source(source, sys.stdin, sys.stdout, sys.stderr))
"""


def compile_source(source_path: pathlib.Path, output_path: pathlib.Path) -> None:
    source = source_path.read_text(encoding="utf-8")
    parse_source(source)
    output_path.write_text(wrapper_for_source(source), encoding="utf-8")
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
