"""Nos testes, as requisições entram como o administrador (sem passar pelo Google), a não ser nos testes de
contas (marcados com `contas`), que usam as sessões de verdade."""

import pytest

from cardline import auth


def pytest_configure(config):
    config.addinivalue_line("markers", "contas: usa o login de verdade (sem entrar automaticamente)")


@pytest.fixture(autouse=True)
def logged_in(request, monkeypatch):
    if request.node.get_closest_marker("contas"):
        return

    def as_admin(con, settings, token):
        user = auth.admin(con, settings) or auth.upsert_user(con, "teste@cardline.local")
        auth.adopt(con, settings)  # o que os testes criaram sem dono fica com ele
        return user

    monkeypatch.setattr(auth, "request_user", as_admin)
