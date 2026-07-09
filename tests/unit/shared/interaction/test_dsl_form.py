"""Unit tests for Interaction Service — field serialization (dsl.py)."""

from __future__ import annotations

import pytest

from src.shared.interaction.field_base import BaseField
from src.shared.interaction.fields import (
    CheckboxField,
    DateField,
    NumberField,
    RadioField,
    SelectField,
    TextAreaField,
    TextField,
)
from src.shared.interaction.form import Form


# ── Fixtures: form classes ──────────────────────────────────────────────


class EmptyForm(Form):
    pass


class SimpleForm(Form):
    nome = TextField(label="Nome", required=True, placeholder="João da Silva")
    idade = NumberField(label="Idade", min=18, max=120, value=30)


class FullForm(Form):
    nome = TextField(
        label="Nome",
        required=True,
        placeholder="João da Silva",
        hint="Nome completo",
        max_length=255,
        min_length=3,
        width="full",
        group="dados_pessoais",
        order=1,
        icon="user",
    )
    idade = NumberField(
        label="Idade",
        min=18,
        max=120,
        value=30,
        width="third",
        group="dados_pessoais",
        order=2,
    )
    obs = TextAreaField(
        label="Observações",
        rows=6,
        max_length=1000,
        required=False,
    )
    setor = SelectField(
        label="Setor",
        required=True,
        options=[
            {"value": "ti", "label": "Tecnologia da Informação"},
            {"value": "rh", "label": "Recursos Humanos"},
        ],
    )
    ativo = CheckboxField(label="Ativo", value=True)
    cor = RadioField(
        label="Cor preferida",
        options=[
            {"value": "azul", "label": "Azul"},
            {"value": "verde", "label": "Verde"},
        ],
    )
    nascimento = DateField(
        label="Data de nascimento",
        min_date="1900-01-01",
        max_date="2026-12-31",
    )


class ConditionalForm(Form):
    nome = TextField(label="Nome", required=True)
    cpf = TextField(
        label="CPF",
        visible_when={"field": "tipo", "op": "eq", "value": "pf"},
    )
    cnpj = TextField(
        label="CNPJ",
        visible_when={"field": "tipo", "op": "eq", "value": "pj"},
        enabled_when={"field": "tipo", "op": "eq", "value": "pj"},
    )


# ── Tests: Form.__init_subclass__ field collection ─────────────────────


class TestFormSubclass:
    """Field collection via __init_subclass__."""

    def test_empty_form_has_no_fields(self) -> None:
        assert EmptyForm._fields == {}

    def test_simple_form_collects_fields(self) -> None:
        assert "nome" in SimpleForm._fields
        assert "idade" in SimpleForm._fields
        assert len(SimpleForm._fields) == 2

    def test_field_id_set_from_attribute_name(self) -> None:
        nome_field = SimpleForm._fields["nome"]
        assert nome_field.id == "nome"
        assert nome_field.label == "Nome"

    def test_field_instances_are_correct_types(self) -> None:
        assert isinstance(SimpleForm._fields["nome"], TextField)
        assert isinstance(SimpleForm._fields["idade"], NumberField)


# ── Tests: build_schema (DSL serialization) ────────────────────────────


class TestBuildSchema:
    """Form → InteractionSchema conversion."""

    def test_empty_form_schema(self) -> None:
        schema = EmptyForm.build_schema()
        assert schema.interaction_type == "form"
        assert schema.fields == []

    def test_empty_form_to_dict(self) -> None:
        d = EmptyForm.to_dict()
        assert d["interaction_type"] == "form"
        assert d["fields"] == []

    def test_simple_form_schema_fields(self) -> None:
        schema = SimpleForm.build_schema()
        assert len(schema.fields) == 2
        field_ids = {f.id for f in schema.fields}
        assert field_ids == {"nome", "idade"}

    def test_text_field_schema_properties(self) -> None:
        schema = SimpleForm.build_schema()
        nome_schema = next(f for f in schema.fields if f.id == "nome")
        assert nome_schema.field_type == "text"
        assert nome_schema.required is True
        assert nome_schema.placeholder == "João da Silva"
        assert nome_schema.value is None

    def test_number_field_schema_properties(self) -> None:
        schema = SimpleForm.build_schema()
        idade_schema = next(f for f in schema.fields if f.id == "idade")
        assert idade_schema.field_type == "number"
        assert idade_schema.value == 30
        assert idade_schema.extras.get("min") == 18
        assert idade_schema.extras.get("max") == 120

    def test_full_form_all_field_types(self) -> None:
        schema = FullForm.build_schema()
        field_types = {f.field_type for f in schema.fields}
        assert field_types == {"text", "number", "textarea", "select", "checkbox", "radio", "date"}

    def test_layout_metadata_serialized(self) -> None:
        schema = FullForm.build_schema()
        nome_schema = next(f for f in schema.fields if f.id == "nome")
        assert nome_schema.width == "full"
        assert nome_schema.group == "dados_pessoais"
        assert nome_schema.order == 1
        assert nome_schema.icon == "user"

    def test_select_field_options_serialized(self) -> None:
        schema = FullForm.build_schema()
        setor_schema = next(f for f in schema.fields if f.id == "setor")
        options = setor_schema.extras.get("options", [])
        assert len(options) == 2
        assert options[0]["value"] == "ti"

    def test_checkbox_field_value_serialized(self) -> None:
        schema = FullForm.build_schema()
        ativo_schema = next(f for f in schema.fields if f.id == "ativo")
        assert ativo_schema.value is True

    def test_date_field_extras(self) -> None:
        schema = FullForm.build_schema()
        nasc_schema = next(f for f in schema.fields if f.id == "nascimento")
        assert nasc_schema.extras.get("min_date") == "1900-01-01"
        assert nasc_schema.extras.get("max_date") == "2026-12-31"

    def test_conditional_visibility_serialized(self) -> None:
        schema = ConditionalForm.build_schema()
        cpf_schema = next(f for f in schema.fields if f.id == "cpf")
        assert cpf_schema.visible_when == {"field": "tipo", "op": "eq", "value": "pf"}

    def test_conditional_enabled_serialized(self) -> None:
        schema = ConditionalForm.build_schema()
        cnpj_schema = next(f for f in schema.fields if f.id == "cnpj")
        assert cnpj_schema.enabled_when == {"field": "tipo", "op": "eq", "value": "pj"}

    def test_to_dict_produces_json_compatible_output(self) -> None:
        d = FullForm.to_dict()
        assert isinstance(d, dict)
        assert isinstance(d["fields"], list)
        assert all(isinstance(f, dict) for f in d["fields"])
        # Verify a text field has expected structure
        nome_dict = next(f for f in d["fields"] if f["id"] == "nome")
        assert nome_dict["field_type"] == "text"
        assert nome_dict["required"] is True
        assert nome_dict["max_length"] == 255

    def test_visible_fields_not_filtered_in_schema(self) -> None:
        """Fields with visible=False are still present in the schema
        (the React renderer decides whether to show them)."""
        class FormWithHidden(Form):
            visivel = TextField(label="Visível", visible=True)
            oculto = TextField(label="Oculto", visible=False)

        schema = FormWithHidden.build_schema()
        field_ids = {f.id for f in schema.fields}
        assert "oculto" in field_ids
        oculto = next(f for f in schema.fields if f.id == "oculto")
        assert oculto.visible is False


# ── Tests: FieldSchema.to_dict() ──────────────────────────────────────


class TestFieldSchemaToDict:
    """FieldSchema → dict at serialization boundary."""

    def test_base_field_to_dict_minimal(self) -> None:
        f = BaseField(id="campo", label="Campo", field_type="text")
        d = f.to_dict()
        assert d["id"] == "campo"
        assert d["field_type"] == "text"
        assert d["label"] == "Campo"
        assert d["required"] is False
        assert d["value"] is None

    def test_text_field_to_dict_includes_extras(self) -> None:
        f = TextField(id="nome", label="Nome", max_length=100, min_length=3)
        d = f.to_dict()
        assert d["max_length"] == 100
        assert d["min_length"] == 3

    def test_number_field_to_dict_includes_extras(self) -> None:
        f = NumberField(id="idade", label="Idade", min=0, max=150, step=1)
        d = f.to_dict()
        assert d["min"] == 0
        assert d["max"] == 150
        assert d["step"] == 1
