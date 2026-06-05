"""Round-trip and behaviour tests for the delimited-text columnar codec."""
from compressor import csvcolumnar as CV


def _grid(rows, delim=b";", lt=b"\n", trailing=True):
    body = lt.join(delim.join(r) for r in rows)
    return body + (lt if trailing else b"")


def test_roundtrip_numeric_decimal_text_columns():
    rows = [[b"Date", b"V", b"n"]]
    v = 234000
    for i in range(2000):
        v += (i * 37 % 101) - 50
        rows.append([b"16/12/2006", f"{v/1000:.3f}".encode(), str(i).encode()])
    data = _grid(rows)
    blob = CV.encode(data)
    assert CV.decode(blob) == data
    assert len(blob) < len(data) // 4          # numeric columns crush


def test_fixed_decimal_byte_exact():
    # trailing zeros and 0.000 must come back exactly (canonical reformat)
    rows = [[b"x"], [b"234.840"], [b"0.000"], [b"4.216"], [b"-1.500"]]
    data = _grid(rows)
    assert CV.decode(CV.encode(data)) == data


def test_non_canonical_numbers_fall_to_text():
    # leading zero / plus sign aren't canonical -> coded as text, still exact
    rows = [[b"x"], [b"04.216"], [b"+1.0"], [b"1.50"], [b"1.500"]]
    data = _grid(rows)
    assert CV.decode(CV.encode(data)) == data


def test_crlf_and_no_trailing_newline():
    rows = [[b"a", b"b"], [b"1", b"2"], [b"3", b"4"], [b"5", b"6"]]
    for lt in (b"\n", b"\r\n"):
        for trailing in (True, False):
            data = _grid(rows, lt=lt, trailing=trailing)
            assert CV.decode(CV.encode(data)) == data


def test_comma_and_tab_delimiters():
    for d in (b",", b"\t", b"|"):
        rows = [[b"h1", b"h2", b"h3"]] + [[str(i).encode(), b"y", str(i * 2).encode()]
                                          for i in range(500)]
        data = _grid(rows, delim=d)
        assert CV.decode(CV.encode(data)) == data


def test_ragged_rows_fall_back():
    # inconsistent field count -> not a grid -> deflate/store, still byte-exact
    data = b"a;b;c\n1;2\n3;4;5;6\n"
    blob = CV.encode(data)
    assert CV.decode(blob) == data
    assert blob[4] != CV.M_GRID                # M_STORE or M_DEFLATE, not grid


def test_quoted_field_changing_field_count_falls_back():
    data = b'a,b\n"x,y",z\np,q\n'              # the quoted comma breaks the field count
    assert CV.decode(CV.encode(data)) == data


def test_empty_and_tiny_and_never_expands():
    for d in (b"", b"x\n", b"a;b\n", b"plain text, no grid\n"):
        blob = CV.encode(d)
        assert CV.decode(blob) == d
        assert len(blob) <= len(d) + 5
