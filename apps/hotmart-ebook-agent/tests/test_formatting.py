"""Formatação brasileira — o relatório é lido por um operador no Brasil."""

from __future__ import annotations

from hotmart_agent.formatting import brl, num, pct, signed_pct, times


def test_moeda():
    assert brl(3146.29) == "R$ 3.146,29"
    assert brl(97) == "R$ 97,00"
    assert brl(1_234_567.8) == "R$ 1.234.567,80"
    assert brl(-250.5) == "-R$ 250,50"
    assert brl(0) == "R$ 0,00"


def test_percentual():
    assert pct(0.1052, 1) == "10,5%"
    assert pct(0.0116, 2) == "1,16%"
    assert pct(0.3, 0) == "30%"


def test_percentual_com_sinal():
    assert signed_pct(-0.204, 1) == "-20,4%"
    assert signed_pct(0.209, 1) == "+20,9%"
    assert signed_pct(0.0, 1) == "+0,0%"


def test_multiplicador():
    assert times(3.4567) == "3,46x"


def test_numero_puro():
    assert num(4148, 0) == "4.148"
    assert num(0.5) == "0,50"
