from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, field_validator

KnowledgeType = Literal[
    "definition",
    "attribute",
    "mechanism",
    "fact",
    "method",
    "constraint",
    "relation",
    "limitation",
]
RelationType = Literal[
    "equivalent",
    "complementary",
    "broader_narrower",
    "conflict",
    "unrelated",
    "uncertain",
]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Evidence(Strict):
    source_span_id: str
    quote: str = Field(min_length=1)

    @field_validator("quote")
    @classmethod
    def meaningful_quote(cls, value):
        if not value.strip():
            raise ValueError("证据引用不能为空白")
        return value


class ClaimDraft(Strict):
    concept: str = Field(min_length=1)
    knowledge_type: KnowledgeType
    statement: str = Field(min_length=1)
    conditions: list[str]
    qualifiers: list[str]
    evidence: list[Evidence] = Field(min_length=1)

    @field_validator("concept", "statement")
    @classmethod
    def meaningful_claim(cls, value):
        if not value.strip():
            raise ValueError("概念和陈述不能为空白")
        return value.strip()


class Extraction(Strict):
    claims: list[ClaimDraft]


class Judgment(Strict):
    relation: RelationType
    reason: str = Field(min_length=1)
    conditions_comparison: str = Field(min_length=1)
    conditions_match: bool
    merge_recommended: bool
    broader_claim_id: str | None = None


class Concept(Strict):
    id: str
    name: str
    knowledge_ids: list[str] = Field(min_length=1)


class Topic(Strict):
    id: str
    name: str
    parent_id: str | None = None
    knowledge_ids: list[str]


class Organization(Strict):
    concepts: list[Concept]
    topics: list[Topic]


class Paragraph(Strict):
    text: str = Field(min_length=1)
    knowledge_ids: list[str] = Field(min_length=1)


class Article(Strict):
    title: str = Field(min_length=1)
    paragraphs: list[Paragraph]
    insufficient_evidence: bool
