"""Tests for the local claim_entailment antibody (stub client, offline)."""
import json

from ollarma.cafa import ClaimEntailmentAntibody, EntailmentVerdict


class StubClient:
    """Mimics ollama.Client().generate -> {'response': <json text>}."""

    def __init__(self, response_text: str, raise_exc: Exception | None = None):
        self.response_text = response_text
        self.raise_exc = raise_exc
        self.last_kwargs = None

    def generate(self, **kwargs):
        self.last_kwargs = kwargs
        if self.raise_exc:
            raise self.raise_exc
        return {"response": self.response_text}


def _v(label, conf=0.9, rat="because"):
    return json.dumps({"label": label, "confidence": conf, "rationale": rat})


def test_support_label_parsed():
    ab = ClaimEntailmentAntibody(StubClient(_v("SUPPORT")))
    v = ab.judge(target="P1", claim="X does Y", abstract="X performs Y in cells.")
    assert isinstance(v, EntailmentVerdict)
    assert v.label == "SUPPORT" and 0.0 <= v.confidence <= 1.0
    assert v.quarantined is False
    assert v.authority == "advisory"          # open calibration -> advisory
    assert v.frontier_used is False           # local-only


def test_refute_and_noinfo():
    assert ClaimEntailmentAntibody(StubClient(_v("REFUTE"))).judge(
        target="P1", claim="c", abstract="a").label == "REFUTE"
    assert ClaimEntailmentAntibody(StubClient(_v("NOINFO"))).judge(
        target="P1", claim="c", abstract="a").label == "NOINFO"


def test_fixture_claim_abstract_pairs_flow_through_public_api():
    fixtures = [
        (
            "P12345",
            "Protein X increases autophagy in neuronal cells.",
            "We show that Protein X induces autophagy markers LC3-II and p62 turnover.",
            "SUPPORT",
        ),
        (
            "P67890",
            "Protein Y is required for mitochondrial fission.",
            "Protein Y knockout cells display normal mitochondrial fission rates.",
            "REFUTE",
        ),
        (
            "P99999",
            "Protein Z binds prion aggregates.",
            "This abstract describes cell-cycle checkpoints and does not mention Protein Z.",
            "NOINFO",
        ),
    ]
    for target, claim, abstract, label in fixtures:
        client = StubClient(_v(label))
        verdict = ClaimEntailmentAntibody(client).judge(
            target=target, claim=claim, abstract=abstract)
        assert verdict.label == label
        assert verdict.target == target
        assert claim in client.last_kwargs["prompt"]
        assert abstract in client.last_kwargs["prompt"]


def test_json_mode_schema_passed_to_client():
    c = StubClient(_v("SUPPORT"))
    ClaimEntailmentAntibody(c, model="qwen3.5:9b").judge(
        target="P1", claim="c", abstract="a")
    assert c.last_kwargs["model"] == "qwen3.5:9b"
    assert c.last_kwargs["format"]["properties"]["label"]["enum"] == [
        "SUPPORT", "REFUTE", "NOINFO"]
    assert c.last_kwargs["options"]["temperature"] == 0.0


def test_missing_abstract_fail_closed():
    v = ClaimEntailmentAntibody(StubClient(_v("SUPPORT"))).judge(
        target="P1", claim="c", abstract="")
    assert v.label == "NOINFO" and v.quarantined is True


def test_unparseable_output_quarantined():
    v = ClaimEntailmentAntibody(StubClient("not json at all")).judge(
        target="P1", claim="c", abstract="a")
    assert v.label == "NOINFO" and v.quarantined is True


def test_invalid_label_quarantined():
    v = ClaimEntailmentAntibody(StubClient(_v("MAYBE"))).judge(
        target="P1", claim="c", abstract="a")
    assert v.label == "NOINFO" and v.quarantined is True


def test_model_error_fail_closed():
    ab = ClaimEntailmentAntibody(StubClient("", raise_exc=RuntimeError("daemon down")))
    v = ab.judge(target="P1", claim="c", abstract="a")
    assert v.label == "NOINFO" and v.quarantined is True
    assert "model error" in v.rationale


def test_json_embedded_in_prose_recovered():
    ab = ClaimEntailmentAntibody(StubClient('Sure! ' + _v("SUPPORT") + ' done'))
    v = ab.judge(target="P1", claim="c", abstract="a")
    assert v.label == "SUPPORT"


def test_confidence_clamped():
    ab = ClaimEntailmentAntibody(StubClient(_v("SUPPORT", conf=5.0)))
    assert ab.judge(target="P1", claim="c", abstract="a").confidence == 1.0


def test_non_finite_confidence_quarantined():
    ab = ClaimEntailmentAntibody(
        StubClient('{"label":"SUPPORT","confidence":NaN,"rationale":"bad"}'))
    v = ab.judge(target="P1", claim="c", abstract="a")
    assert v.label == "NOINFO"
    assert v.confidence == 0.0
    assert v.quarantined is True
