"""BBC BASIC II tokenizer and detokenizer for the STARMAP fixture.

Program format, as BBC BASIC II stores it in memory and SAVE writes it:

* each line is ``0x0D, line_hi, line_lo, length, body`` where ``length``
  counts the whole line, the four prefix bytes included (so at most 255);
* the program ends with ``0x0D 0xFF``.

The keyword table and the tokenising rules follow the BBC BASIC II ROM
(keyword table at ``TOKENS`` and the ``MATCH`` routine in the annotated
disassembly at https://github.com/ivop/bbc-basic, based on J.G. Harston's
reconstruction). Token values were cross-checked against the ROM-verified
table at https://fileformats.org/code/program/bbc/basic/.

Deliberate limits, each raising :class:`TokenizeError` rather than producing
bytes that differ from what the ROM would store:

* line-number references (a number after GOTO, GOSUB, THEN, ELSE, RESTORE,
  TRACE, AUTO, DELETE, LIST or RENUMBER) would need the ``0x8D`` encoding,
  which is not implemented;
* keyword abbreviations such as ``P.`` are not supported;
* source text must be printable ASCII.

Source format: one line per text line, ``<line number> <body>``. One space
between the line number and the body is a separator and is not stored; any
further spaces are part of the body. Blank lines are ignored.
:func:`normalise` gives the canonical text, and
``detokenize(tokenize(src)) == normalise(src)`` for every accepted source.
"""

from __future__ import annotations

__all__ = [
    "KEYWORDS",
    "MAX_LINE_NUMBER",
    "TOKEN_TEXT",
    "TokenizeError",
    "detokenize",
    "normalise",
    "tokenize",
]

MAX_LINE_NUMBER = 32767
MAX_LINE_LENGTH = 255

# Flag bits, as in the ROM keyword table.
_F_CONDITIONAL = 0x01  # not tokenised if followed by a word character
_F_MIDDLE = 0x02  # enter "middle of statement" mode
_F_START = 0x04  # enter "start of statement" mode
_F_PROC_NAME = 0x08  # FN/PROC: the following name is not tokenised
_F_LINE_NUMBER = 0x10  # a line number may follow
_F_REST_RAW = 0x20  # REM/DATA: rest of the line is not tokenised
_F_PSEUDO_VAR = 0x40  # add 0x40 to the token at the start of a statement

# (keyword, token, flags) in ROM table order. The tokenizer takes the first
# entry that matches, and the ROM orders entries so that a keyword always
# precedes any shorter keyword that is its prefix (ENDPROC before END, GET$
# before GET, MODE before MOD), so the first match is the longest match.
KEYWORDS: tuple[tuple[str, int, int], ...] = (
    ("AND", 0x80, 0x00),
    ("ABS", 0x94, 0x00),
    ("ACS", 0x95, 0x00),
    ("ADVAL", 0x96, 0x00),
    ("ASC", 0x97, 0x00),
    ("ASN", 0x98, 0x00),
    ("ATN", 0x99, 0x00),
    ("AUTO", 0xC6, 0x10),
    ("BGET", 0x9A, 0x01),
    ("BPUT", 0xD5, 0x03),
    ("COLOUR", 0xFB, 0x02),
    ("CALL", 0xD6, 0x02),
    ("CHAIN", 0xD7, 0x02),
    ("CHR$", 0xBD, 0x00),
    ("CLEAR", 0xD8, 0x01),
    ("CLOSE", 0xD9, 0x03),
    ("CLG", 0xDA, 0x01),
    ("CLS", 0xDB, 0x01),
    ("COS", 0x9B, 0x00),
    ("COUNT", 0x9C, 0x01),
    ("DATA", 0xDC, 0x20),
    ("DEG", 0x9D, 0x00),
    ("DEF", 0xDD, 0x00),
    ("DELETE", 0xC7, 0x10),
    ("DIV", 0x81, 0x00),
    ("DIM", 0xDE, 0x02),
    ("DRAW", 0xDF, 0x02),
    ("ENDPROC", 0xE1, 0x01),
    ("END", 0xE0, 0x01),
    ("ENVELOPE", 0xE2, 0x02),
    ("ELSE", 0x8B, 0x14),
    ("EVAL", 0xA0, 0x00),
    ("ERL", 0x9E, 0x01),
    ("ERROR", 0x85, 0x04),
    ("EOF", 0xC5, 0x01),
    ("EOR", 0x82, 0x00),
    ("ERR", 0x9F, 0x01),
    ("EXP", 0xA1, 0x00),
    ("EXT", 0xA2, 0x01),
    ("FOR", 0xE3, 0x02),
    ("FALSE", 0xA3, 0x01),
    ("FN", 0xA4, 0x08),
    ("GOTO", 0xE5, 0x12),
    ("GET$", 0xBE, 0x00),
    ("GET", 0xA5, 0x00),
    ("GOSUB", 0xE4, 0x12),
    ("GCOL", 0xE6, 0x02),
    ("HIMEM", 0x93, 0x43),
    ("INPUT", 0xE8, 0x02),
    ("IF", 0xE7, 0x02),
    ("INKEY$", 0xBF, 0x00),
    ("INKEY", 0xA6, 0x00),
    ("INT", 0xA8, 0x00),
    ("INSTR(", 0xA7, 0x00),
    ("LIST", 0xC9, 0x10),
    ("LINE", 0x86, 0x00),
    ("LOAD", 0xC8, 0x02),
    ("LOMEM", 0x92, 0x43),
    ("LOCAL", 0xEA, 0x02),
    ("LEFT$(", 0xC0, 0x00),
    ("LEN", 0xA9, 0x00),
    ("LET", 0xE9, 0x04),
    ("LOG", 0xAB, 0x00),
    ("LN", 0xAA, 0x00),
    ("MID$(", 0xC1, 0x00),
    ("MODE", 0xEB, 0x02),
    ("MOD", 0x83, 0x00),
    ("MOVE", 0xEC, 0x02),
    ("NEXT", 0xED, 0x02),
    ("NEW", 0xCA, 0x01),
    ("NOT", 0xAC, 0x00),
    ("OLD", 0xCB, 0x01),
    ("ON", 0xEE, 0x02),
    ("OFF", 0x87, 0x00),
    ("OR", 0x84, 0x00),
    ("OPENIN", 0x8E, 0x00),
    ("OPENOUT", 0xAE, 0x00),
    ("OPENUP", 0xAD, 0x00),
    ("OSCLI", 0xFF, 0x02),
    ("PRINT", 0xF1, 0x02),
    ("PAGE", 0x90, 0x43),
    ("PTR", 0x8F, 0x43),
    ("PI", 0xAF, 0x01),
    ("PLOT", 0xF0, 0x02),
    ("POINT(", 0xB0, 0x00),
    ("PROC", 0xF2, 0x0A),
    ("POS", 0xB1, 0x01),
    ("RETURN", 0xF8, 0x01),
    ("REPEAT", 0xF5, 0x00),
    ("REPORT", 0xF6, 0x01),
    ("READ", 0xF3, 0x02),
    ("REM", 0xF4, 0x20),
    ("RUN", 0xF9, 0x01),
    ("RAD", 0xB2, 0x00),
    ("RESTORE", 0xF7, 0x12),
    ("RIGHT$(", 0xC2, 0x00),
    ("RND", 0xB3, 0x01),
    ("RENUMBER", 0xCC, 0x10),
    ("STEP", 0x88, 0x00),
    ("SAVE", 0xCD, 0x02),
    ("SGN", 0xB4, 0x00),
    ("SIN", 0xB5, 0x00),
    ("SQR", 0xB6, 0x00),
    ("SPC", 0x89, 0x00),
    ("STR$", 0xC3, 0x00),
    ("STRING$(", 0xC4, 0x00),
    ("SOUND", 0xD4, 0x02),
    ("STOP", 0xFA, 0x01),
    ("TAN", 0xB7, 0x00),
    ("THEN", 0x8C, 0x14),
    ("TO", 0xB8, 0x00),
    ("TAB(", 0x8A, 0x00),
    ("TRACE", 0xFC, 0x12),
    ("TIME", 0x91, 0x43),
    ("TRUE", 0xB9, 0x01),
    ("UNTIL", 0xFD, 0x02),
    ("USR", 0xBA, 0x00),
    ("VDU", 0xEF, 0x02),
    ("VAL", 0xBB, 0x00),
    ("VPOS", 0xBC, 0x01),
    ("WIDTH", 0xFE, 0x02),
)

_PSEUDO_OFFSET = 0x40
_LINE_NUMBER_TOKEN = 0x8D

TOKEN_TEXT: dict[int, str] = {token: word for word, token, _ in KEYWORDS}
for _word, _token, _flags in KEYWORDS:
    if _flags & _F_PSEUDO_VAR:
        # Statement forms of PTR, PAGE, TIME, LOMEM, HIMEM (0xCF..0xD3).
        TOKEN_TEXT[_token + _PSEUDO_OFFSET] = _word
del _word, _token, _flags

_HEX_DIGITS = frozenset("0123456789ABCDEF")


class TokenizeError(ValueError):
    """Source text or program bytes outside what this tokenizer supports."""


def _is_word_char(ch: str) -> bool:
    # ROM WORDCQ: 0-9, A-Z, and '_' through 'z' (which includes '`').
    return ("0" <= ch <= "9") or ("A" <= ch <= "Z") or ("_" <= ch <= "z")


def _word_end(text: str, i: int) -> int:
    while i < len(text) and _is_word_char(text[i]):
        i += 1
    return i


def _parse_source(source: str) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    previous = -1
    for row, raw in enumerate(source.split("\n"), start=1):
        if raw.endswith("\r"):
            raw = raw[:-1]
        if not raw.strip():
            continue
        for ch in raw:
            if not (" " <= ch <= "~"):
                raise TokenizeError(
                    f"source line {row}: character {ch!r} is not printable ASCII"
                )
        text = raw.lstrip(" ")
        digits_end = 0
        while digits_end < len(text) and text[digits_end].isdigit():
            digits_end += 1
        if digits_end == 0:
            raise TokenizeError(f"source line {row}: line does not start with a line number")
        number = int(text[:digits_end])
        if number > MAX_LINE_NUMBER:
            raise TokenizeError(
                f"source line {row}: line number {number} exceeds {MAX_LINE_NUMBER}"
            )
        if number <= previous:
            raise TokenizeError(
                f"source line {row}: line number {number} does not follow {previous}"
            )
        previous = number
        body = text[digits_end:]
        if body.startswith(" "):
            body = body[1:]
        lines.append((number, body))
    return lines


def _match_keyword(body: str, i: int, line_no: int) -> tuple[str, int, int] | None:
    """First keyword-table entry matching at ``body[i]``, as the ROM does it."""
    first = body[i]
    for entry in KEYWORDS:
        word = entry[0]
        if word[0] != first:
            continue
        k = 1
        while k < len(word):
            ch = body[i + k] if i + k < len(body) else ""
            if ch != word[k]:
                if ch == ".":
                    raise TokenizeError(
                        f"line {line_no}: keyword abbreviation {body[i:i + k + 1]!r} "
                        "is not supported"
                    )
                break
            k += 1
        else:
            return entry
    return None


def _tokenize_body(body: str, line_no: int) -> bytes:
    out = bytearray()
    n = len(body)
    i = 0
    start = True  # at the start of a statement
    line_numbers = False  # a number here would be a line-number reference

    def copy(end: int) -> None:
        out.extend(body[i:end].encode("ascii"))

    while i < n:
        ch = body[i]
        if ch == " " or ch == ",":
            copy(i + 1)
            i += 1
        elif ch == "&":
            j = i + 1
            while j < n and body[j] in _HEX_DIGITS:
                j += 1
            copy(j)
            i = j
        elif ch == '"':
            close = body.find('"', i + 1)
            j = n if close < 0 else close + 1
            copy(j)
            i = j
        elif ch == ":":
            copy(i + 1)
            i += 1
            start, line_numbers = True, False
        elif ch == "*" and start:
            copy(n)  # star command: passed to the OS untokenised
            i = n
        elif ch == "." or ch.isdigit():
            if line_numbers and ch.isdigit():
                raise TokenizeError(
                    f"line {line_no}: line-number references are not supported"
                )
            j = i
            while j < n and (body[j].isdigit() or body[j] == "."):
                j += 1
            copy(j)
            i = j
            start, line_numbers = False, False
        elif "A" <= ch <= "W" and (entry := _match_keyword(body, i, line_no)) is not None:
            word, token, flags = entry
            j = i + len(word)
            if flags & _F_CONDITIONAL and j < n and _is_word_char(body[j]):
                end = _word_end(body, i)  # a variable name, e.g. ENDX
                copy(end)
                i = end
                start, line_numbers = False, False
                continue
            if flags & _F_PSEUDO_VAR and start:
                token += _PSEUDO_OFFSET
            out.append(token)
            i = j
            if flags & _F_MIDDLE:
                start, line_numbers = False, False
            if flags & _F_START:
                start, line_numbers = True, False
            if flags & _F_PROC_NAME:
                end = _word_end(body, i)
                copy(end)
                i = end
            if flags & _F_LINE_NUMBER:
                line_numbers = True
            if flags & _F_REST_RAW:
                copy(n)
                i = n
        elif _is_word_char(ch):
            end = _word_end(body, i)  # variable name: keywords inside it stay text
            copy(end)
            i = end
            start, line_numbers = False, False
        else:
            copy(i + 1)
            i += 1
            start, line_numbers = False, False
    return bytes(out)


def tokenize(source: str) -> bytes:
    """Tokenize BBC BASIC source into the BBC BASIC II program format."""
    program = bytearray()
    for number, body in _parse_source(source):
        encoded = _tokenize_body(body, number)
        length = 4 + len(encoded)
        if length > MAX_LINE_LENGTH:
            raise TokenizeError(
                f"line {number}: tokenized line is {length} bytes, limit {MAX_LINE_LENGTH}"
            )
        program += bytes((0x0D, number >> 8, number & 0xFF, length))
        program += encoded
    program += b"\x0d\xff"
    return bytes(program)


def _detokenize_body(body: bytes, line_no: int) -> str:
    parts: list[str] = []
    in_quotes = False
    for byte in body:
        if byte >= 0x80 and not in_quotes:
            if byte == _LINE_NUMBER_TOKEN:
                raise TokenizeError(
                    f"line {line_no}: line-number references (0x8D) are not supported"
                )
            text = TOKEN_TEXT.get(byte)
            if text is None:
                raise TokenizeError(f"line {line_no}: unknown token 0x{byte:02X}")
            parts.append(text)
        elif 0x20 <= byte <= 0x7E:
            if byte == 0x22:
                in_quotes = not in_quotes
            parts.append(chr(byte))
        else:
            raise TokenizeError(
                f"line {line_no}: byte 0x{byte:02X} is not printable ASCII"
            )
    return "".join(parts)


def detokenize(program: bytes) -> str:
    """Turn a tokenized program back into normalised source text."""
    lines: list[str] = []
    previous = -1
    i = 0
    size = len(program)
    while True:
        if i >= size:
            raise TokenizeError("program ends without the 0x0D 0xFF terminator")
        if program[i] != 0x0D:
            raise TokenizeError(f"expected 0x0D at offset {i}, found 0x{program[i]:02X}")
        if i + 1 < size and program[i + 1] == 0xFF:
            if i + 2 != size:
                raise TokenizeError(f"{size - i - 2} bytes follow the program terminator")
            break
        if i + 4 > size:
            raise TokenizeError(f"truncated line header at offset {i}")
        number = (program[i + 1] << 8) | program[i + 2]
        length = program[i + 3]
        if number > MAX_LINE_NUMBER:
            raise TokenizeError(f"line number {number} at offset {i} exceeds {MAX_LINE_NUMBER}")
        if number <= previous:
            raise TokenizeError(f"line number {number} at offset {i} does not follow {previous}")
        if length < 4 or i + length > size:
            raise TokenizeError(f"line {number}: invalid length byte {length}")
        body = program[i + 4 : i + length]
        if 0x0D in body:
            raise TokenizeError(f"line {number}: 0x0D inside the line body")
        text = _detokenize_body(body, number)
        lines.append(f"{number} {text}" if text else str(number))
        previous = number
        i += length
    return "".join(line + "\n" for line in lines)


def normalise(source: str) -> str:
    """Canonical form of accepted source text, as :func:`detokenize` returns it."""
    return "".join(
        (f"{number} {body}" if body else str(number)) + "\n"
        for number, body in _parse_source(source)
    )
