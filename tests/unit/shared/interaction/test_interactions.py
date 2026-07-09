"""Unit tests for Interaction Service — FormInteraction and BaseInteraction."""

from __future__ import annotations

import pytest

from src.shared.interaction.enums import InteractionType, ResumeMode
from src.shared.interaction.interactions import BaseInteraction, FormInteraction
from src.shared.interaction.form import Form
from src.shared.interaction.fields import TextField, NumberField


class ClienteForm(Form):
    nome = TextField(label="Nome", required=True)
    idade = NumberField(label="Idade", min=18)


class TestBaseInteraction:
    """BaseInteraction — the abstract foundation."""

    def test_cannot_instantiate_and_call_to_dict(self) -> None:
        """BaseInteraction.to_dict() raises NotImplementedError.
        Instantiation is allowed (dataclasses don't enforce abstract methods),
        but calling to_dict() on a subclass that doesn't override it fails.
        """
        class Incomplete(BaseInteraction):
            pass

        obj = Incomplete(interaction_type=InteractionType.FORM, title="Test")
        with pytest.raises(NotImplementedError):
            obj.to_dict()


class TestFormInteraction:
    """FormInteraction — the V1 concrete type."""

    def test_instantiate_with_form_class(self) -> None:
        fi = FormInteraction(
            interaction_type=InteractionType.FORM,
            title="Cadastro",
            description="Preencha os dados",
            form=ClienteForm,
        )
        assert fi.interaction_type == InteractionType.FORM
        assert fi.title == "Cadastro"

    def test_to_dict_produces_correct_structure(self) -> None:
        fi = FormInteraction(
            interaction_type=InteractionType.FORM,
            title="Cadastro",
            description="Preencha os dados",
            form=ClienteForm,
            submit_label="Cadastrar",
            cancel_label="Voltar",
            size="lg",
        )
        d = fi.to_dict()
        assert d["interaction_type"] == "form"
        assert d["title"] == "Cadastro"
        assert d["description"] == "Preencha os dados"
        assert d["submit_label"] == "Cadastrar"
        assert d["cancel_label"] == "Voltar"
        assert d["size"] == "lg"
        assert d["timeout_seconds"] == 600
        assert isinstance(d["fields"], list)
        assert len(d["fields"]) == 2

    def test_to_dict_fields_are_correct(self) -> None:
        fi = FormInteraction(
            interaction_type=InteractionType.FORM,
            title="Cadastro",
            form=ClienteForm,
        )
        d = fi.to_dict()
        field_ids = {f["id"] for f in d["fields"]}
        assert field_ids == {"nome", "idade"}

    def test_default_ui_hints(self) -> None:
        fi = FormInteraction(
            interaction_type=InteractionType.FORM,
            title="Test",
            form=ClienteForm,
        )
        d = fi.to_dict()
        assert d["submit_label"] is None
        assert d["cancel_label"] is None
        assert d["size"] == "md"

    def test_schema_override_used_instead_of_form(self) -> None:
        override = {"fields": [{"id": "x", "field_type": "text", "label": "X"}]}
        fi = FormInteraction(
            interaction_type=InteractionType.FORM,
            title="Override",
            schema_override=override,
        )
        d = fi.to_dict()
        assert d["fields"] == override["fields"]

    def test_requires_form_or_schema_override(self) -> None:
        fi = FormInteraction(
            interaction_type=InteractionType.FORM,
            title="No source",
        )
        with pytest.raises(ValueError, match="requires `form` or `schema_override`"):
            fi.to_dict()
