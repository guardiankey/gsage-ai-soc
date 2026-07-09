"""gSage AI — Interaction Service public API.

Usage in tools::

    from src.shared.interaction import (
        Form, TextField, NumberField, SelectField,
        FormInteraction, ResumeMode,
    )

    class MeuForm(Form):
        nome = TextField(label="Nome", required=True)
        idade = NumberField(label="Idade", min=18)

    class MinhaTool(BaseTool):
        async def execute(self, agent_context, params, config, state):
            dados = await self.interaction.form(
                MeuForm,
                title="Cadastro",
                resume=ResumeMode.CONTINUE_TOOL,
            )
            nome = dados["nome"]
"""

from src.shared.interaction.enums import (  # noqa: F401
    InteractionStatus,
    InteractionType,
    ResumeMode,
)
from src.shared.interaction.exceptions import (  # noqa: F401
    InteractionCancelled,
    InteractionError,
    InteractionReplanRequested,
    InteractionTimeout,
)
from src.shared.interaction.field_base import (  # noqa: F401
    BaseField,
    FieldSchema,
    InteractionResponseData,
    InteractionSchema,
)
from src.shared.interaction.fields import (  # noqa: F401
    CheckboxField,
    CheckboxGroupField,
    DateField,
    NumberField,
    RadioField,
    SelectField,
    TextAreaField,
    TextField,
)
from src.shared.interaction.form import Form  # noqa: F401
from src.shared.interaction.interactions import (  # noqa: F401
    BaseInteraction,
    FormInteraction,
)
from src.shared.interaction.broker import InteractionBroker  # noqa: F401
from src.shared.interaction.service import InteractionService  # noqa: F401
