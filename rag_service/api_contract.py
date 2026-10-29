"""Public Agent v1 wire models. Internal objects must pass through this boundary."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, StrictFloat, StrictInt, model_validator, field_validator
from .contracts import METRICS, normalize_condition

RULE_SCHEMA_VERSION = 'equipment-rules-v1'
DeviceType = Literal['motor', 'pump', 'fan', 'coupling', 'gearbox']


class Output(BaseModel):
    # Strip new internal fields rather than publishing them accidentally.
    model_config = ConfigDict(extra='ignore')


class Source(Output):
    document_id: str
    document_revision: int
    title: str
    sha256: str
    locator: str
    original_quote: str


class ConditionNode(BaseModel):
    model_config = ConfigDict(extra='forbid', allow_inf_nan=False)


class Leaf(ConditionNode):
    metric: str
    label: str | None = None
    ge: StrictInt | StrictFloat | None = None
    gt: StrictInt | StrictFloat | None = None
    le: StrictInt | StrictFloat | None = None
    lt: StrictInt | StrictFloat | None = None


class AllCondition(ConditionNode):
    all: list['Leaf | AllCondition | AnyCondition'] = Field(min_length=1)


class AnyCondition(ConditionNode):
    any: list['Leaf | AllCondition | AnyCondition'] = Field(min_length=1)


AllCondition.model_rebuild()
AnyCondition.model_rebuild()
Condition = Leaf | AllCondition | AnyCondition


class Rule(Output):
    id: str
    knowledge_revision: int
    name: str
    device_type: DeviceType
    original_text: str
    condition: Condition
    units: dict[str, str]
    phenomena: list[str]
    causes: list[str]
    actions: list[str]
    notes: list[str]
    review_notes: list[str]
    # Preserve known optional metadata, without leaking future internal fields.
    source_section: str | None = None
    source_locator: str | None = None
    block_ids: list[str] | None = None
    legacy_source: dict | None = None
    source: dict | None = None
    review_status: str | None = None
    valid: bool | None = None
    validation_errors: list[str] | None = None
    retired: bool | None = None

    @model_validator(mode='after')
    def consistent_units(self):
        def visit(node):
            if 'metric' in node:
                metric = node['metric']
                expected = METRICS[metric][1] if metric in METRICS else '1'
                if self.units.get(metric) != expected:
                    raise ValueError('规则单位与v1指标约定不符')
            else:
                for child in node.get('all', node.get('any', [])):
                    visit(child)
        visit(self.condition.model_dump(exclude_unset=True))
        return self

    @field_validator('condition', mode='before')
    @classmethod
    def valid_condition(cls, value):
        normalize_condition(value)
        return value


class ApplicableRule(Rule):
    knowledge_id: str
    source: Source


class Knowledge(Output):
    id: str
    kind: Literal['description', 'rule']
    text: str
    device_type: DeviceType | None = None
    knowledge_revision: int
    source: Source
    rule: Rule | None = None

    @model_validator(mode='after')
    def rule_required(self):
        if self.kind == 'rule' and self.rule is None:
            raise ValueError('专业规则条目缺少rule')
        return self


class Hit(Knowledge):
    semantic_score: float = Field(allow_inf_nan=False)
    keyword_score: float = Field(allow_inf_nan=False)
    score: float = Field(allow_inf_nan=False)


class KnowledgeBase(Output):
    id: str
    name: str
    archived: int
    current_release: str | None
    state_version: int


class Retrieval(Output):
    kb_id: str
    release_id: str
    profile: dict
    items: list[Hit]


class Citation(Output):
    knowledge_id: str
    quote: str
    source: Source


class Answer(Retrieval):
    status: Literal['answered', 'insufficient_evidence']
    answer: str
    citations: list[Citation]


class Rules(Output):
    kb_id: str
    release_id: str
    schema_version: Literal['equipment-rules-v1']
    rules: list[ApplicableRule]
    count: int


class KnowledgeDetail(Output):
    release_id: str
    item: Knowledge


class Metric(Output):
    name: str
    unit: str


class DerivedMetric(Metric):
    numerators: list[str]
    denominator: str
    formula: str


class Capabilities(Output):
    schema_version: Literal['equipment-rules-v1']
    device_types: dict[str, str]
    metrics: dict[str, Metric]
    derived: dict[str, DerivedMetric]
    operators: list[str]
    groups: list[str]
    formats: list[str]
    executes_diagnosis: Literal[False]


class Health(Output):
    status: Literal['ok']
    embedding: dict
    rule_schema_version: Literal['equipment-rules-v1']


class Error(Output):
    # Preserve the existing detail string / FastAPI validation-list convention.
    detail: str | list[dict]
    code: str


ERROR_CODES = {400: 'BAD_REQUEST', 401: 'UNAUTHORIZED', 403: 'FORBIDDEN',
               404: 'NOT_FOUND', 409: 'CONFLICT', 422: 'VALIDATION_ERROR'}
ERROR_RESPONSES = {status: {'model': Error} for status in (*ERROR_CODES, 500)}
