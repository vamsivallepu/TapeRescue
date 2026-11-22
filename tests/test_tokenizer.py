"""Unit tests for the BBC BASIC II tokenizer (task 6.1, Requirement 4.2)."""

import pytest

from taperescue.fixtures.tokenizer import (
    KEYWORDS,
    TOKEN_TEXT,
    TokenizeError,
    detokenize,
    normalise,
    tokenize,
)


def body_of(source: str) -> bytes:
    """Body bytes of a one-line program, after checking the line framing."""
    program = tokenize(source)
    assert program[0] == 0x0D
    assert program[-2:] == b"\x0d\xff"
    length = program[3]
    assert length == len(program) - 2, "length byte counts the whole line"
    return program[4:length]


# Token values from the BBC BASIC II ROM keyword table (see the verification entry).
EXPECTED_TOKENS = {
    "PRINT": 0xF1, "MODE": 0xEB, "DEF": 0xDD, "PROC": 0xF2, "END": 0xE0,
    "FOR": 0xE3, "NEXT": 0xED, "GCOL": 0xE6, "MOVE": 0xEC, "DRAW": 0xDF,
    "PLOT": 0xF0, "VDU": 0xEF, "READ": 0xF3, "DATA": 0xDC, "REM": 0xF4,
    "RESTORE": 0xF7, "RND": 0xB3, "ENDPROC": 0xE1, "TO": 0xB8, "STEP": 0x88,
    "IF": 0xE7, "THEN": 0x8C, "ELSE": 0x8B, "COLOUR": 0xFB, "CLS": 0xDB,
    "LOCAL": 0xEA, "SIN": 0xB5, "COS": 0x9B, "INT": 0xA8, "AND": 0x80,
    "OR": 0x84, "NOT": 0xAC, "REPEAT": 0xF5, "UNTIL": 0xFD, "GOTO": 0xE5,
    "GOSUB": 0xE4,
}


class TestKeywordTable:
    def test_named_tokens_have_rom_values(self):
        table = {word: token for word, token, _ in KEYWORDS}
        for word, token in EXPECTED_TOKENS.items():
            assert table[word] == token, word

    def test_tokens_are_unique_and_cover_the_rom_range(self):
        tokens = [token for _, token, _ in KEYWORDS]
        assert len(tokens) == len(set(tokens))
        # 0x80..0xFF minus 0x8D (line number), 0xCE (unused) and the five
        # statement-form pseudo-variables, which are reached via the 0x40 offset.
        assert set(TOKEN_TEXT) == set(range(0x80, 0x100)) - {0x8D, 0xCE}

    def test_first_match_is_longest_match(self):
        words = [word for word, _, _ in KEYWORDS]
        for i, short in enumerate(words):
            for long in words[i + 1 :]:
                assert not long.startswith(short), f"{short} precedes {long}"

    @pytest.mark.parametrize(
        ("source", "expected"),
        [
            ("10 ENDPROC", b"\xe1"),
            ("10 END", b"\xe0"),
            ("10 MODE 1", b"\xeb 1"),
            ("10 X=Y MOD 2", b"X=Y \x83 2"),
            ("10 A$=GET$", b"A$=\xbe"),
            ("10 A=GET", b"A=\xa5"),
        ],
    )
    def test_longer_keyword_wins(self, source, expected):
        assert body_of(source) == expected


class TestLineFormat:
    def test_single_line_bytes(self):
        assert tokenize('10 PRINT "HI"') == bytes(
            [0x0D, 0x00, 0x0A, 0x0A, 0xF1, 0x20, 0x22, 0x48, 0x49, 0x22, 0x0D, 0xFF]
        )

    def test_line_number_high_byte_first(self):
        program = tokenize("300 CLS")
        assert program[:4] == bytes([0x0D, 0x01, 0x2C, 5])

    def test_empty_program(self):
        assert tokenize("") == b"\x0d\xff"
        assert detokenize(b"\x0d\xff") == ""

    def test_multiple_lines_and_terminator(self):
        program = tokenize("10 CLS\n20 END\n")
        assert program == bytes([0x0D, 0, 10, 5, 0xDB, 0x0D, 0, 20, 5, 0xE0, 0x0D, 0xFF])

    def test_separator_space_not_stored_but_indent_kept(self):
        assert body_of("10PRINT") == b"\xf1"
        assert body_of("10 PRINT") == b"\xf1"
        assert body_of("10   PRINT") == b"  \xf1"


class TestUntokenisedText:
    def test_strings_are_not_tokenised(self):
        assert body_of('10 PRINT "PRINT MODE"') == b'\xf1 "PRINT MODE"'

    def test_rest_after_rem_is_raw(self):
        assert body_of("10 REM PRINT MODE") == b"\xf4 PRINT MODE"

    def test_rest_after_data_is_raw(self):
        assert body_of("10 DATA ORION,PRINT,10") == b"\xdc ORION,PRINT,10"

    def test_proc_name_is_not_tokenised(self):
        assert body_of("10 DEF PROCEND") == b"\xdd \xf2END"
        assert body_of("10 PROCconstellations") == b"\xf2constellations"

    def test_keyword_inside_variable_name(self):
        assert body_of("10 XPRINT=1") == b"XPRINT=1"
        assert body_of("10 print=1") == b"print=1"

    def test_conditional_keyword_followed_by_letter_is_a_variable(self):
        assert body_of("10 ENDX=1") == b"ENDX=1"
        assert body_of("10 X=RND(6)") == b"X=\xb3(6)"

    def test_unconditional_keyword_at_start_of_name_is_tokenised(self):
        # ROM behaviour: TO has no conditional flag, so TOTAL is TO + TAL.
        assert body_of("10 TOTAL=1") == b"\xb8TAL=1"

    def test_hex_constant_is_not_tokenised(self):
        assert body_of("10 X=&DEF") == b"X=&DEF"

    def test_star_command_at_statement_start(self):
        assert body_of("10 *FX 200,1") == b"*FX 200,1"

    def test_pseudo_variable_statement_and_function_forms(self):
        assert body_of("10 TIME=0") == b"\xd1=0"
        assert body_of("10 PRINT TIME") == b"\xf1 \x91"
        assert body_of("10 X=1:TIME=0") == b"X=1:\xd1=0"


class TestRejections:
    @pytest.mark.parametrize(
        "source",
        ["10 GOTO 20", "10 GOSUB 100", "10 IF X THEN 20", "10 RESTORE 100",
         "10 ON X GOSUB 100,200", "10 IF X THEN PRINT ELSE 30"],
    )
    def test_line_number_references_rejected(self, source):
        with pytest.raises(TokenizeError, match="line-number references"):
            tokenize(source)

    def test_restore_without_number_is_allowed(self):
        assert body_of("10 RESTORE") == b"\xf7"

    def test_abbreviation_rejected(self):
        with pytest.raises(TokenizeError, match="abbreviation"):
            tokenize('10 P."HI"')

    @pytest.mark.parametrize(
        "source",
        ["PRINT", "20 CLS\n10 CLS", "10 CLS\n10 CLS", "32768 END",
         "10 PRINT \"\u00a3\"", "10 PRINT\t1", "10 REM " + "X" * 250],
    )
    def test_bad_source_rejected(self, source):
        with pytest.raises(TokenizeError):
            tokenize(source)

    def test_longest_allowed_line(self):
        program = tokenize("10 REM " + "X" * 249)
        assert program[3] == 255

    @pytest.mark.parametrize(
        "program",
        [b"", b"\x0d\x00\x0a\x05\xf1", b"\x0d\xff\x00", b"\x0d\x00\x0a\x06\xe5\x8d\x0d\xff",
         b"\x0d\x00\x0a\x05\xce\x0d\xff", b"\x0d\x00\x0a\x03\x0d\xff",
         b"\x0d\x00\x14\x04\x0d\x00\x0a\x04\x0d\xff"],
    )
    def test_bad_program_rejected(self, program):
        with pytest.raises(TokenizeError):
            detokenize(program)


SAMPLE = """\
10 REM STARMAP sample
20 MODE 1:VDU 23,1,0;0;0;0;
30 X=RND(-42)
40 FOR I%=1 TO 100 STEP 2
50   GCOL 0,RND(3):PLOT 69,RND(1279),RND(1023)
60 NEXT
70 PROCconstellations
80 COLOUR 3:PRINT TAB(10,1)"STAR MAP"
90 END
100 DEF PROCconstellations
110 LOCAL A,B
120 RESTORE
130 REPEAT READ A,B:IF A>=0 THEN MOVE A,B:DRAW A+INT(SIN(B)*50),B+INT(COS(A)*50)
140 UNTIL A<0 OR NOT TRUE
150 ENDPROC
160 DATA 100,200,300,400,-1,-1
"""


def test_round_trip_sample_program():
    program = tokenize(SAMPLE)
    assert detokenize(program) == normalise(SAMPLE) == SAMPLE


def test_round_trip_normalises_spacing_and_blank_lines():
    source = "  10PRINT\r\n\n20 \n30  CLS"
    assert normalise(source) == "10 PRINT\n20\n30  CLS\n"
    assert detokenize(tokenize(source)) == normalise(source)
