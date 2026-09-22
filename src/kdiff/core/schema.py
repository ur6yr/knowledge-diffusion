"""P1 S2 field names; P3 Table 11 signatures, plus explicit P1 additions."""

from enum import StrEnum


class EntityType(StrEnum):
    AUTHOR = "Author"
    PAPER = "Paper"
    INSTITUTION = "Institution"
    TOPIC = "Topic"
    PATENT = "Patent"
    GRANT = "Grant"


SCHEMA_VERSION = "kdiff-0.1"
IDENTITY_VERSION = "source-identity-v1"

# Optional means unknown is allowed, never synthesized from model memory.
FIELDS = {
    "Author": "authorID name orcidID email careerStage researchInterests hIndex citationCount homepage gender nationality annotation".split(),
    "Paper": "paperID title abstract doi arxivID publicationYear publicationMonth venue venueType impactFactor pages volume issue keywords citationCount openAccess fundingAcknowledgment annotation".split(),
    "Institution": "institutionID rorID name aliases type carnegieClassification location coordinates foundedYear website parentInstitution endowment studentCount facultyCount ranking annotation".split(),
    "Patent": "patentID patentNumber title abstract filingDate publicationDate grantDate expirationDate assignee inventors classificationCodes patentOffice legalStatus citationCount nonPatentCitations annotation".split(),
    "Topic": "topicID name parentTopic childTopics keywords openAlexID wikipediaURL fieldOfStudy emergenceYear peakYear trendScore annotation".split(),
    "Grant": "grantID grantNumber title abstract fundingAgency program amount currency startDate endDate principalInvestigator coInvestigators hostInstitution keywords deliverables annotation".split(),
}

# (head, allowed tails, required qualifier, source basis)
RELATIONS = {
    "authorOf": ("Author", ("Paper",), None, "P1 S2/P3 T11"),
    "affiliatedWith": ("Author", ("Institution",), None, "P1 S2/P3 T11"),
    "classifiedAs": ("Paper", ("Topic",), None, "P3 T11"),
    "citesPaper": ("Paper", ("Paper",), None, "P3 T11; P1 cites alias"),
    "citedByPatent": ("Paper", ("Patent",), "date_basis", "P1 S2/P3 T11; patent cites paper"),
    "citesPatent": ("Paper", ("Patent",), None, "P1 S2; paper cites patent"),
    "assignedTo": ("Patent", ("Institution",), None, "P1 S2/P3 T11"),
    "fundedBy": ("Paper", ("Grant",), None, "P3 T11; inverse P1 supportsPaper"),
    "grantedBy": ("Grant", ("Institution",), "role", "P3 T11"),
    "advisorOf": ("Author", ("Author",), None, "P1 S2/P3 T11"),
    "collaboratesWith": ("Author", ("Author",), "contributing_ids", "P1 S2/P3 T11"),
    "hasSite": ("Institution", ("Institution",), "site", "P3 T11; operational"),
    "announcedSite": ("Institution", ("Institution",), "site", "P3 T11; announcement only"),
}
for _name, _head, _tails in [
    ("corresponding", "Author", ("Paper",)),
    ("coauthoredWith", "Author", ("Author",)),
    ("committeeMembers", "Author", ("Author",)),
    ("visitingAt", "Author", ("Institution",)),
    ("obtainedDegreeAt", "Author", ("Institution",)),
    ("emeritusAt", "Author", ("Institution",)),
    ("extends", "Paper", ("Paper",)), ("refutes", "Paper", ("Paper",)),
    ("updates", "Paper", ("Paper",)), ("enablesTechnology", "Paper", ("Patent",)),
    ("worksIn", "Author", ("Topic",)), ("pioneeredTopic", "Author", ("Topic",)),
    ("subunitOf", "Institution", ("Institution",)),
    ("mergedWith", "Institution", ("Institution",)),
    ("consortium", "Institution", ("Institution",)),
    ("funds", "Grant", ("Author", "Institution")),
    ("inventedBy", "Patent", ("Author",)),
    ("licenses", "Patent", ("Institution",)),
    ("priorityClaimFrom", "Patent", ("Patent",)),
]:
    RELATIONS[_name] = (_head, _tails, None, "P1 S2; see D02")

ALIASES = {
    "cites": {"canonical": "citesPaper", "inverse": False},
    "supportsPaper": {"canonical": "fundedBy", "inverse": True},
}
# Diagram-only ambiguous labels (researchTopic, citedBy, grant arrows) are
# deliberately not automatically mapped; a source adapter must supply semantics.


def validate_relation(relation, head, tail, qualifiers):
    if relation not in RELATIONS:
        raise ValueError(f"Unregistered relation: {relation}")
    h, ts, required, _ = RELATIONS[relation]
    if head.kind != h or tail.kind not in ts:
        raise ValueError("Relation endpoint types do not match registry")
    if required == "site":
        if tail.subtype != "site":
            raise ValueError("Site target must be an Institution with site subtype")
    elif required and not qualifiers.get(required):
        raise ValueError(f"Relation requires {required}")
    if relation == "grantedBy" and qualifiers["role"] not in {"sponsor", "administrator"}:
        raise ValueError("Grant role must distinguish sponsor and administrator")
