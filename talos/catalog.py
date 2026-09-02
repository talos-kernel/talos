"""Der Anbieter-Katalog — reine Daten darueber, wer Talos denken lassen kann.

Talos kann nur so viele Anbieter bedienen, weil fast alle **OpenAI-kompatibel** sind: ein
einziger Reasoner (`ApiReasoner`) spricht zwei Protokolle — `anthropic` nativ und `openai`
fuer alles andere — und der Unterschied zwischen zwei Anbietern schrumpft damit auf drei
Angaben: Basis-URL, Name der Schluessel-Variablen, Modellname. Genau das steht hier.

**Daten, kein Verhalten.** Dieses Modul ruft nichts im Netz auf, liest keine Datei und
meldet sich nirgends an. Es kennt keinen einzigen Schluessel — nur den *Namen* der
Umgebungsvariablen, in der der Betreiber seinen eigenen ablegt. Wer hier Anmeldelogik
ergaenzt, macht aus einer Liste eine Angriffsflaeche.

**Leere Felder sind Absicht, keine Luecke im Datensatz.** Eine falsche `base_url` ist
schlimmer als eine fehlende: sie sieht aus wie Unterstuetzung und scheitert erst beim
Nutzer, mit einem Fehler, der nach seinem Schluessel aussieht statt nach unserem Fehler.
Dasselbe gilt fuer Modell-IDs — die wechseln bei manchen Anbietern im Monatsrhythmus.
Was nicht belegt ist, bleibt leer und steht in `notes`. `has_models` und
`without_models()` machen diese Luecken sichtbar, statt sie zu verstecken.

**Der Katalog vergibt keine Rechte.** Er sagt, wie man einen Anbieter *erreicht*, nie ob
man ihn benutzen darf. Ueber das Denken entscheidet weiterhin der Betreiber per Schluessel,
ueber Werkzeuge ausschliesslich der Policy-Kernel.
"""
from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AUTH_KINDS",
    "WIRE_KINDS",
    "MODEL_INFO",
    "MODEL_INFO_FIELDS",
    "PROVIDERS",
    "ModelInfo",
    "ProviderInfo",
    "get",
    "by_auth",
    "by_wire",
    "model_ids",
    "model_info",
    "slugs",
    "without_models",
]

# `oauth-device` = Code im Browser bestaetigen, `oauth-pkce` = Umleitung auf localhost.
# `cli` = eine fremde Hersteller-CLI besitzt die Anmeldung; Talos fasst sie nie an.
AUTH_KINDS: frozenset[str] = frozenset(
    {"oauth-device", "oauth-pkce", "api-key", "local", "cli"}
)

# Das Protokoll, das der Reasoner sprechen muss. Mehr gibt es nicht — und das ist der
# Grund, warum diese Liste so lang sein darf.
WIRE_KINDS: frozenset[str] = frozenset({"anthropic", "openai"})

# Nur diese Schemata sind ein Endpunkt. `file:` oder ein blosser Hostname waeren ein
# stiller Konfigurationsfehler, der erst im Netzaufruf auffaellt.
_URL_SCHEMES: tuple[str, ...] = ("https://", "http://")

# Ein Schluessel wird ueber die Umgebung gereicht — bei genau diesen Verfahren.
_KEYED_AUTH: frozenset[str] = frozenset({"api-key"})


@dataclass(frozen=True)
class ProviderInfo:
    """Ein Anbieter, so wie ihn ein Reasoner erreichen kann. Unveraenderlich.

    `base_url` leer heisst: die Vorgabe des Protokolls gilt (`ApiReasoner` setzt sie).
    `models` leer heisst: es gibt keine belegte Liste — nicht, dass es keine Modelle gibt.
    """

    slug: str
    label: str
    auth: str
    wire: str = ""
    base_url: str = ""
    env_key: str = ""
    models: tuple[str, ...] = ()
    notes: str = ""

    @property
    def has_models(self) -> bool:
        """Gibt es eine belegte Modell-Liste? Falsch heisst: der Nutzer muss sie nennen."""
        return bool(self.models)

    @property
    def needs_key(self) -> bool:
        return self.auth in _KEYED_AUTH


# Die Felder, die ein Modell beschreiben — und die EINZIGEN, die ein Betreiber per
# `TALOS_MODEL_OVERRIDES` korrigieren darf. Geschlossen, damit ein Override nie
# Anbieter, Adresse oder Schluesselnamen tragen kann: das waere ein zweiter Weg zu
# Rechten, am Katalog und an `credentials.py` vorbei (siehe `modelinfo.py`).
MODEL_INFO_FIELDS: tuple[str, ...] = (
    "context_window", "input_price", "output_price", "vision", "reasoning",
)


@dataclass(frozen=True)
class ModelInfo:
    """Eckdaten eines Modells. Unveraenderlich.

    `0` und `False` heissen UNBEKANNT — nie „gratis" und nie „kann es nicht". Ein
    Verbraucher, der aus 0 einen Preis von null macht, erfindet eine Zahl; deshalb
    fragt er `has_prices`, nicht den Wert. Preise gelten je Million Token in USD,
    das Fenster in Token.

    `overridden` nennt die Felder, die vom Betreiber stammen. Die Anzeige braucht das,
    damit niemand den Katalog fuer die Quelle einer Zahl haelt, die er nie enthielt.
    """

    context_window: int = 0
    input_price: float = 0.0
    output_price: float = 0.0
    vision: bool = False
    reasoning: bool = False
    overridden: frozenset[str] = frozenset()

    @property
    def has_prices(self) -> bool:
        """Gibt es einen belegten Preis? Auch „0, vom Betreiber gesetzt" zaehlt: ein
        lokales Modell kostet wirklich nichts, und das ist eine Auskunft, kein Loch."""
        return bool({"input_price", "output_price"} & self.overridden) or (
            self.input_price > 0 or self.output_price > 0
        )

    @property
    def known(self) -> bool:
        return bool(self.overridden) or bool(
            self.context_window or self.input_price or self.output_price
            or self.vision or self.reasoning
        )


# --- Der Katalog ----------------------------------------------------------------------
#
# Reihenfolge: OAuth/Abo, dann eigener Schluessel, dann lokal, dann CLI. Innerhalb der
# Gruppen grob nach Bekanntheit — der Einrichtungs-Assistent zeigt sie in dieser Folge.

PROVIDERS: tuple[ProviderInfo, ...] = (
    # --- OAuth / Abo ------------------------------------------------------------------
    ProviderInfo(
        slug="nous-portal",
        label="Nous Portal",
        auth="oauth-device",
        wire="openai",
        notes="Sign in to the Nous portal; the endpoint and model list are not documented here yet.",
    ),
    ProviderInfo(
        slug="openai-codex",
        label="OpenAI Codex (ChatGPT sign-in)",
        auth="cli",
        notes='The ChatGPT-subscription endpoint appears only as a constant in the Codex CLI, in no public API documentation, and OpenAI declined to confirm whether third-party use is permitted. Drive the official codex binary instead.',
    ),
    ProviderInfo(
        slug="github-copilot",
        label="GitHub Copilot",
        auth="cli",
        notes='The raw endpoint relies on unpublished APIs, which the Copilot developer policy forbids by name; Copilot Extensions were retired in Nov 2025. The supported route is the official Copilot SDK, which needs the Copilot CLI installed.',
    ),
    ProviderInfo(
        slug="anthropic-max",
        label="Anthropic (Claude Max subscription)",
        auth="cli",
        models=(
            "claude-opus-5",
            "claude-fable-5",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-fable-5-1",
        ),
        notes='Anthropic forbids third-party OAuth for subscription plans: it does not permit developers to route requests through Free, Pro or Max credentials on behalf of users. The permitted route is the official claude CLI, which the operator logs into themselves. Do not turn this back into oauth.',
    ),
    ProviderInfo(
        slug="google-gemini-oauth",
        label="Google Gemini (Google account)",
        auth="cli",
        notes='Google stopped serving the consumer Code Assist tiers on 2026-06-18, which ended the Gemini CLI OAuth path; the endpoint was never publicly documented. Use an API key, Vertex, or the vendor CLI.',
    ),
    ProviderInfo(
        slug="xai-grok-oauth",
        label="xAI Grok (SuperGrok subscription)",
        auth="oauth-device",
        wire="openai",
        notes="Uses a SuperGrok subscription; for pay-as-you-go use the xai API-key entry instead.",
    ),
    ProviderInfo(
        slug="qwen-portal",
        label="Qwen Portal",
        auth="cli",
        notes='The Qwen OAuth free tier was discontinued on 2026-04-15 and its endpoints were never vendor-documented. Cataloguing it as OAuth would look like support and fail at the user. Use DashScope with a key instead.',
    ),
    ProviderInfo(
        slug="minimax-oauth",
        label="MiniMax (account sign-in)",
        auth="oauth-pkce",
        wire="openai",
        notes="Signs in with a MiniMax account; for a key-based setup use the minimax entry.",
    ),
    # --- Eigener API-Schluessel -------------------------------------------------------
    ProviderInfo(
        slug="openrouter",
        label="OpenRouter",
        auth="api-key",
        wire="openai",
        base_url="https://openrouter.ai/api/v1",
        env_key="OPENROUTER_API_KEY",
        notes="One key for hundreds of models; pick the model id from openrouter.ai/models.",
    ),
    ProviderInfo(
        slug="openai-api",
        label="OpenAI API (your own key)",
        auth="api-key",
        wire="openai",
        env_key="OPENAI_API_KEY",
        # Handkuratiert und identisch mit `OPENAI_API_MODELS` in `provider.py` — nicht
        # geraten, sondern die Liste, die diese Installation ohnehin schon ausliefert.
        models=("gpt-5.6-sol", "gpt-5.2", "gpt-5-codex", "o4-mini"),
        notes="Needs a paid OpenAI API key; the default endpoint applies.",
    ),
    ProviderInfo(
        slug="anthropic-api",
        label="Anthropic API (your own key)",
        auth="api-key",
        wire="anthropic",
        env_key="ANTHROPIC_API_KEY",
        models=(
            "claude-opus-5",
            "claude-fable-5",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-fable-5-1",
        ),
        notes="Needs an Anthropic API key; billed per token, separate from a Claude subscription.",
    ),
    ProviderInfo(
        slug="google-gemini-api",
        label="Google Gemini API",
        auth="api-key",
        wire="openai",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        env_key="GEMINI_API_KEY",
        notes="Get a key from Google AI Studio; this is Gemini's OpenAI-compatible endpoint.",
    ),
    ProviderInfo(
        slug="deepseek",
        label="DeepSeek",
        auth="api-key",
        wire="openai",
        base_url="https://api.deepseek.com/v1",
        env_key="DEEPSEEK_API_KEY",
        models=("deepseek-chat", "deepseek-reasoner"),
        notes="Needs a DeepSeek platform key; deepseek-reasoner is the thinking model.",
    ),
    ProviderInfo(
        slug="xai",
        label="xAI (API key)",
        auth="api-key",
        wire="openai",
        base_url="https://api.x.ai/v1",
        env_key="XAI_API_KEY",
        notes="Pay-as-you-go xAI key; model ids are listed in the xAI console.",
    ),
    ProviderInfo(
        slug="novita",
        label="Novita AI",
        auth="api-key",
        wire="openai",
        base_url="https://api.novita.ai/openai",
        env_key="NOVITA_API_KEY",
        notes="Needs a Novita key; model ids are listed in their model library.",
    ),
    ProviderInfo(
        slug="zai-glm",
        label="z.ai (GLM)",
        auth="api-key",
        wire="openai",
        base_url="https://api.z.ai/api/paas/v4",
        env_key="ZAI_API_KEY",
        notes="Needs a z.ai key for the GLM models; a coding-plan key uses a different endpoint path.",
    ),
    ProviderInfo(
        slug="kimi-moonshot",
        label="Kimi / Moonshot AI (global)",
        auth="api-key",
        wire="openai",
        base_url="https://api.moonshot.ai/v1",
        env_key="MOONSHOT_API_KEY",
        notes="Global Moonshot endpoint; accounts created in China use the kimi-china entry.",
    ),
    ProviderInfo(
        slug="kimi-china",
        label="Kimi / Moonshot AI (China)",
        auth="api-key",
        wire="openai",
        base_url="https://api.moonshot.cn/v1",
        env_key="MOONSHOT_API_KEY",
        notes="Mainland China endpoint; keys are not interchangeable with the global one.",
    ),
    ProviderInfo(
        slug="kimi",
        label="Kimi (coding plan)",
        auth="api-key",
        wire="openai",
        base_url="https://api.kimi.com/coding/v1",
        env_key="KIMI_API_KEY",
        notes="Kimi coding endpoint; needs its own KIMI_API_KEY, the Moonshot keys do not apply here.",
    ),
    ProviderInfo(
        slug="minimax",
        label="MiniMax (API key, global)",
        auth="api-key",
        wire="openai",
        base_url="https://api.minimax.io/v1",
        env_key="MINIMAX_API_KEY",
        notes="Global MiniMax endpoint; a key issued in China will not work against it.",
    ),
    ProviderInfo(
        slug="minimax-china",
        label="MiniMax (API key, China)",
        auth="api-key",
        wire="openai",
        env_key="MINIMAX_API_KEY",
        notes="Mainland China account; endpoint differs from the global one and is not listed here.",
    ),
    ProviderInfo(
        slug="alibaba-dashscope",
        label="Alibaba Cloud DashScope (Qwen)",
        auth="api-key",
        wire="openai",
        base_url="https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
        env_key="DASHSCOPE_API_KEY",
        notes="International endpoint; mainland accounts use dashscope.aliyuncs.com instead.",
    ),
    ProviderInfo(
        slug="alibaba-coding-plan",
        label="Alibaba Qwen Coding Plan",
        auth="api-key",
        wire="openai",
        env_key="DASHSCOPE_API_KEY",
        notes="Subscription plan on top of a DashScope key; its endpoint is not documented here.",
    ),
    ProviderInfo(
        slug="tencent-tokenhub",
        label="Tencent TokenHub",
        auth="api-key",
        wire="openai",
        env_key="TENCENT_API_KEY",
        notes="Needs a TokenHub key; the endpoint differs per region, so enter it from your console.",
    ),
    ProviderInfo(
        slug="xiaomi-mimo",
        label="Xiaomi MiMo",
        auth="api-key",
        wire="openai",
        base_url="https://api.xiaomimimo.com/v1",
        env_key="MIMO_API_KEY",
        notes="Needs a Xiaomi MiMo key from their open platform console.",
    ),
    ProviderInfo(
        slug="nvidia-nim",
        label="NVIDIA NIM",
        auth="api-key",
        wire="openai",
        base_url="https://integrate.api.nvidia.com/v1",
        env_key="NVIDIA_API_KEY",
        # Handkuratiert wie die anderen Listen: ein schneller und ein grosser Name,
        # beides belegte IDs aus dem NIM-Katalog. Der Rest kommt live ueber
        # `talos models --refresh` — geraten wird hier nichts.
        models=("meta/llama-3.3-70b-instruct", "nvidia/llama-3.3-nemotron-super-49b-v1.5"),
        notes="Needs an NVIDIA build.nvidia.com key; pick a model id from their catalog.",
    ),
    ProviderInfo(
        slug="aws-bedrock",
        label="AWS Bedrock",
        auth="api-key",
        wire="anthropic",
        env_key="AWS_BEARER_TOKEN_BEDROCK",
        notes="Region-specific endpoint and usually SigV4 signing — a plain bearer key is not enough.",
    ),
    ProviderInfo(
        slug="azure-ai-foundry",
        label="Azure AI Foundry",
        auth="api-key",
        wire="openai",
        env_key="AZURE_API_KEY",
        notes="The endpoint belongs to your own Azure resource, so it must be entered by hand.",
    ),
    ProviderInfo(
        slug="huggingface",
        label="Hugging Face Inference",
        auth="api-key",
        wire="openai",
        base_url="https://router.huggingface.co/v1",
        env_key="HF_TOKEN",
        notes="Needs a Hugging Face token; the model id carries the provider suffix, e.g. :groq.",
    ),
    ProviderInfo(
        slug="arcee-ai",
        label="Arcee AI",
        auth="api-key",
        wire="openai",
        base_url="https://conductor.arcee.ai/v1",
        env_key="ARCEE_API_KEY",
        notes="Needs an Arcee Conductor key; model ids are listed in their console.",
    ),
    ProviderInfo(
        slug="gmi-cloud",
        label="GMI Cloud",
        auth="api-key",
        wire="openai",
        base_url="https://api.gmi-serving.com/v1",
        env_key="GMI_API_KEY",
        notes="Needs a GMI Cloud key; model ids are listed in their inference engine console.",
    ),
    ProviderInfo(
        slug="stepfun",
        label="StepFun",
        auth="api-key",
        wire="openai",
        base_url="https://api.stepfun.ai/v1",
        env_key="STEPFUN_API_KEY",
        notes="Global StepFun endpoint; a key issued on their China platform needs api.stepfun.com instead.",
    ),
    ProviderInfo(
        slug="kilo-code",
        label="Kilo Code",
        auth="api-key",
        wire="openai",
        base_url="https://api.kilo.ai/api/gateway",
        env_key="KILO_API_KEY",
        notes="Needs a Kilo AI Gateway key; one key routes to many models.",
    ),
    ProviderInfo(
        slug="opencode-zen",
        label="OpenCode Zen",
        auth="api-key",
        wire="openai",
        base_url="https://opencode.ai/zen/v1",
        env_key="OPENCODE_API_KEY",
        notes="Needs an OpenCode Zen key; model ids are listed at opencode.ai/docs/zen.",
    ),
    ProviderInfo(
        slug="opencode-go",
        label="OpenCode Go",
        auth="api-key",
        wire="openai",
        base_url="https://opencode.ai/zen/go/v1",
        env_key="OPENCODE_API_KEY",
        notes="Subscription plan with a fixed model set; uses the same OpenCode key as Zen.",
    ),
    ProviderInfo(
        slug="ollama-cloud",
        label="Ollama Cloud",
        auth="api-key",
        wire="openai",
        base_url="https://ollama.com/v1",
        env_key="OLLAMA_API_KEY",
        notes="Hosted Ollama models; needs an ollama.com key, unlike the local Ollama entry.",
    ),
    ProviderInfo(
        slug="perplexity",
        label="Perplexity",
        auth="api-key",
        wire="openai",
        base_url="https://api.perplexity.ai",
        env_key="PERPLEXITY_API_KEY",
        notes="Needs a Perplexity API key; its models answer with web sources.",
    ),
    ProviderInfo(
        slug="mistral",
        label="Mistral AI",
        auth="api-key",
        wire="openai",
        base_url="https://api.mistral.ai/v1",
        env_key="MISTRAL_API_KEY",
        notes="Needs a Mistral platform key; model ids are listed in their console.",
    ),
    ProviderInfo(
        slug="cohere",
        label="Cohere",
        auth="api-key",
        wire="openai",
        base_url="https://api.cohere.ai/compatibility/v1",
        env_key="COHERE_API_KEY",
        notes="Needs a Cohere key; this is their OpenAI-compatible endpoint, not the native one.",
    ),
    ProviderInfo(
        slug="groq",
        label="Groq",
        auth="api-key",
        wire="openai",
        base_url="https://api.groq.com/openai/v1",
        env_key="GROQ_API_KEY",
        notes="Needs a Groq Cloud key; very fast, model ids are listed in their console.",
    ),
    ProviderInfo(
        slug="cloudflare-workers-ai",
        label="Cloudflare Workers AI",
        auth="api-key",
        wire="openai",
        env_key="CLOUDFLARE_API_TOKEN",
        notes="The endpoint contains your Cloudflare account id, so it must be entered by hand.",
    ),
    ProviderInfo(
        slug="together-ai",
        label="Together AI",
        auth="api-key",
        wire="openai",
        base_url="https://api.together.xyz/v1",
        env_key="TOGETHER_API_KEY",
        notes="Needs a Together key; model ids are listed in their model catalog.",
    ),
    ProviderInfo(
        slug="fireworks-ai",
        label="Fireworks AI",
        auth="api-key",
        wire="openai",
        base_url="https://api.fireworks.ai/inference/v1",
        env_key="FIREWORKS_API_KEY",
        notes="Needs a Fireworks key; model ids start with accounts/fireworks/models/.",
    ),
    ProviderInfo(
        slug="cerebras",
        label="Cerebras",
        auth="api-key",
        wire="openai",
        base_url="https://api.cerebras.ai/v1",
        env_key="CEREBRAS_API_KEY",
        notes="Needs a Cerebras Cloud key; model ids are listed in their console.",
    ),
    ProviderInfo(
        slug="sambanova",
        label="SambaNova",
        auth="api-key",
        wire="openai",
        base_url="https://api.sambanova.ai/v1",
        env_key="SAMBANOVA_API_KEY",
        notes="Needs a SambaNova Cloud key; model ids are listed in their console.",
    ),
    ProviderInfo(
        slug="google-vertex-ai",
        label="Google Vertex AI",
        auth="api-key",
        wire="openai",
        env_key="GOOGLE_APPLICATION_CREDENTIALS",
        notes="Endpoint depends on your GCP project and region, and it normally wants a service account rather than a key.",
    ),
    # --- Lokal ------------------------------------------------------------------------
    ProviderInfo(
        slug="ollama",
        label="Ollama (local)",
        auth="local",
        wire="openai",
        base_url="http://localhost:11434/v1",
        notes="Runs on your own machine; start Ollama and pull a model first, no key needed.",
    ),
    ProviderInfo(
        slug="lm-studio",
        label="LM Studio (local)",
        auth="local",
        wire="openai",
        base_url="http://localhost:1234/v1",
        notes="Start the LM Studio local server and load a model; no key needed.",
    ),
    ProviderInfo(
        slug="custom",
        label="Custom OpenAI-compatible endpoint",
        auth="local",
        wire="openai",
        notes="Enter any OpenAI-compatible base URL and model id yourself; nothing is assumed.",
    ),
    # --- Hersteller-CLI ---------------------------------------------------------------
    # `wire` bleibt leer: die CLI besitzt die Verbindung, Talos spricht hier kein
    # Protokoll. Ein geratenes "openai" wuerde eine Weiche im Aufrufer still falsch
    # stellen — leer laesst sie laut auffallen.
    ProviderInfo(
        slug="claude-cli",
        label="Claude CLI (subscription login)",
        auth="cli",
        models=(
            "claude-opus-5",
            "claude-fable-5",
            "claude-sonnet-5",
            "claude-opus-4-8",
            "claude-sonnet-4-6",
            "claude-haiku-4-5",
            "claude-fable-5-1",
        ),
        notes="Requires the claude CLI installed and logged in on this machine.",
    ),
    ProviderInfo(
        slug="hermes",
        label="Hermes CLI",
        auth="cli",
        notes="Requires the hermes CLI installed and configured on this machine.",
    ),
)


# --- Modell-Eckdaten -------------------------------------------------------------------
#
# ABSICHTLICH LEER, aus demselben Grund, aus dem eine `base_url` leer bleiben darf:
# Preise und Fenster wechseln bei manchen Anbietern im Monatsrhythmus, und eine
# veraltete Zahl hier saehe aus wie Wissen. `/usage` rechnete dann mit einem Tarif,
# den es nicht mehr gibt, und niemand koennte sagen, woher er stammt. Wer die Zahlen
# braucht, traegt sie in `TALOS_MODEL_OVERRIDES` ein (`modelinfo.py`) — die Anzeige
# nennt sie dann als Wort des Betreibers, nicht als unseres. Kommt hier je ein
# belegter Eintrag hinzu, legt sich der Override Feld fuer Feld darueber; er ersetzt
# den Eintrag nie ganz und legt nie einen neuen an.
MODEL_INFO: dict[str, ModelInfo] = {}

_UNKNOWN_MODEL = ModelInfo()


def model_info(model_id: str) -> ModelInfo:
    """Was der ausgelieferte Katalog ueber ein Modell sagt — oder `ModelInfo()`.

    Nie `None`: der Aufrufer soll Felder lesen koennen, ohne vorher zu fragen, ob es
    den Eintrag gibt. Unbekannt heisst hier 0/False, und die Verbraucher pruefen das
    (`has_prices`), statt mit einer Null zu rechnen.
    """
    return MODEL_INFO.get(model_id, _UNKNOWN_MODEL)


def model_ids() -> tuple[str, ...]:
    """Jede Modell-ID, die der ausgelieferte Katalog nennt — einmal, in Katalogfolge."""
    return tuple(dict.fromkeys(model for provider in PROVIDERS for model in provider.models))


# --- Zugriff --------------------------------------------------------------------------


def get(slug: str) -> ProviderInfo | None:
    """Ein Anbieter — oder `None`. Wirft nie.

    Ein unbekannter Name ist hier keine Ausnahme, sondern der Normalfall: der Name kommt
    aus einer Konfigurationsdatei oder von einem Menschen, und ein Tippfehler darf den
    Agenten nicht mitnehmen. Der Aufrufer entscheidet, was ein fehlender Eintrag bedeutet.
    """
    return _BY_SLUG.get(slug)


def by_auth(auth: str) -> tuple[ProviderInfo, ...]:
    """Alle Anbieter mit diesem Anmeldeverfahren, in Katalogreihenfolge."""
    return tuple(provider for provider in PROVIDERS if provider.auth == auth)


def by_wire(wire: str) -> tuple[ProviderInfo, ...]:
    """Alle Anbieter, die dieses Protokoll sprechen, in Katalogreihenfolge."""
    return tuple(provider for provider in PROVIDERS if provider.wire == wire)


def slugs() -> tuple[str, ...]:
    return tuple(provider.slug for provider in PROVIDERS)


def without_models() -> tuple[ProviderInfo, ...]:
    """Anbieter ohne belegte Modell-Liste — der Nutzer muss die ID selbst nennen.

    Absichtlich abfragbar: der Einrichtungs-Assistent soll hier nach einem Modellnamen
    fragen, statt eine erfundene ID vorzuschlagen.
    """
    return tuple(provider for provider in PROVIDERS if not provider.models)


# --- Pruefung beim Import -------------------------------------------------------------


def _check_one(provider: ProviderInfo) -> None:
    """Ein Eintrag gegen die Regeln des Datenmodells. Wirft `ValueError`."""
    where = provider.slug or "<leerer slug>"
    if not provider.slug or not provider.label:
        raise ValueError(f"catalog entry {where!r} needs a slug and a label")
    if provider.auth not in AUTH_KINDS:
        raise ValueError(f"catalog entry {where!r} has an unknown auth: {provider.auth!r}")
    if provider.wire and provider.wire not in WIRE_KINDS:
        raise ValueError(f"catalog entry {where!r} has an unknown wire: {provider.wire!r}")
    if not provider.wire and provider.auth != "cli":
        raise ValueError(f"catalog entry {where!r} must name a wire protocol")
    if provider.needs_key and not provider.env_key:
        raise ValueError(f"catalog entry {where!r} needs an env_key")
    if not provider.needs_key and provider.env_key:
        raise ValueError(f"catalog entry {where!r} must not carry an env_key")
    if provider.base_url and not provider.base_url.startswith(_URL_SCHEMES):
        raise ValueError(f"catalog entry {where!r} has a base_url without http(s) scheme")
    if any(not model for model in provider.models):
        raise ValueError(f"catalog entry {where!r} has an empty model id")
    if len(set(provider.models)) != len(provider.models):
        raise ValueError(f"catalog entry {where!r} lists a model twice")
    if not provider.notes:
        raise ValueError(f"catalog entry {where!r} needs a note for the user")


def _check_all(providers: tuple[ProviderInfo, ...]) -> dict[str, ProviderInfo]:
    """Fail-fast beim Import: ein kaputter Katalog darf den Agenten nie erst spaet treffen.

    Ein doppelter Slug ist der teuerste Fehler hier — eine der beiden Zeilen waere fuer
    immer unerreichbar, ohne dass irgendetwas auffaellt.
    """
    if not providers:
        raise ValueError("catalog is empty")
    index: dict[str, ProviderInfo] = {}
    for provider in providers:
        _check_one(provider)
        if provider.slug in index:
            raise ValueError(f"catalog has a duplicate slug: {provider.slug!r}")
        index[provider.slug] = provider
    return index


def _check_model_info(table: dict[str, ModelInfo]) -> None:
    """Dieselbe Disziplin fuer die Modell-Eckdaten. Wirft `ValueError`.

    Ein negativer Preis rechnet still Guthaben aus; ein leerer Name trifft nie. Und
    `overridden` muss im ausgelieferten Bestand leer sein: das Feld gehoert dem
    Betreiber — ein Katalog, der sich als sein Wort ausgibt, verdreht die Kennzeichnung,
    fuer die es gedacht ist.
    """
    for model_id, info in table.items():
        if not model_id or not isinstance(info, ModelInfo):
            raise ValueError(f"catalog model entry {model_id!r} is malformed")
        if info.context_window < 0 or info.input_price < 0 or info.output_price < 0:
            raise ValueError(f"catalog model entry {model_id!r} carries a negative value")
        if info.overridden:
            raise ValueError(f"catalog model entry {model_id!r} must not claim to be an override")


_BY_SLUG: dict[str, ProviderInfo] = _check_all(PROVIDERS)
_check_model_info(MODEL_INFO)
