from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from collections.abc import AsyncIterator
from typing import Any

from agents import Agent, GuardrailFunctionOutput, Runner, SQLiteSession, function_tool, input_guardrail, output_guardrail
from agents.exceptions import InputGuardrailTripwireTriggered, OutputGuardrailTripwireTriggered

from app.config import Settings
from app.db import Database
from app.schemas import AgentAnswer, ChatHistoryItem, ChatSessionDetail
from app.services.knowledge_files import KnowledgeFileService


@dataclass
class AgentContext:
    session_id: str


class AgentService:
    def __init__(self, settings: Settings, db: Database, knowledge_files: KnowledgeFileService):
        self.settings = settings
        self.db = db
        self.knowledge_files = knowledge_files
        self._agent: Agent[AgentContext] | None = None
        self._streaming_agent: Agent[AgentContext] | None = None

    def create_session(self) -> tuple[str, datetime]:
        session_id = str(uuid.uuid4())
        self.db.upsert_session(session_id)
        return session_id, datetime.now(UTC)

    def get_session(self, session_id: str) -> ChatSessionDetail | None:
        row = self.db.get_session(session_id)
        if not row:
            return None
        history = [
            ChatHistoryItem(role=item["role"], content=item["content"], created_at=datetime.fromisoformat(item["created_at"]))
            for item in self.db.get_messages(session_id)
        ]
        return ChatSessionDetail(
            session_id=row["session_id"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            history=history,
        )

    async def ask(self, session_id: str, message: str) -> AgentAnswer:
        self.db.upsert_session(session_id)
        self.db.add_message(session_id, "user", message)
        session = SQLiteSession(session_id, db_path=str(self.settings.session_db_path))
        context = AgentContext(session_id=session_id)

        try:
            result = await Runner.run(
                self._get_agent(),
                message,
                session=session,
                context=context,
            )
            output: AgentAnswer = result.final_output
        except InputGuardrailTripwireTriggered as exc:
            output = AgentAnswer(
                answer="אני יכול לעזור רק בשאלות על הקורסים, השירותים והמידע המאושר של מכללת במבי.",
                citations=[],
                confidence="low",
                needs_human_review=True,
                follow_up_question="איזה קורס או נושא בבמבי מעניין אותך?",
            )
            self.db.add_message(session_id, "system", f"Input guardrail: {exc}")
        except OutputGuardrailTripwireTriggered as exc:
            output = AgentAnswer(
                answer="אין לי כרגע מקור מאושר מספיק כדי לענות בוודאות.",
                citations=[],
                confidence="low",
                needs_human_review=True,
                follow_up_question="אפשר לחדד איזה קורס או פרט אתה צריך?",
            )
            self.db.add_message(session_id, "system", f"Output guardrail: {exc}")

        self.db.add_message(session_id, "assistant", output.model_dump_json(ensure_ascii=False))
        return self._client_output(output)

    async def ask_stream(self, session_id: str, message: str) -> AsyncIterator[dict[str, Any]]:
        self.db.upsert_session(session_id)
        self.db.add_message(session_id, "user", message)
        session = SQLiteSession(session_id, db_path=str(self.settings.session_db_path))
        context = AgentContext(session_id=session_id)

        yield {"type": "status", "message": "מחפש מידע מתאים..."}

        try:
            result = Runner.run_streamed(
                self._get_streaming_agent(),
                message,
                session=session,
                context=context,
            )
            chunks: list[str] = []
            async for event in result.stream_events():
                event_type = getattr(event, "type", "")
                if event_type == "run_item_stream_event":
                    name = getattr(event, "name", "")
                    if name == "tool_called":
                        yield {"type": "status", "message": "בודק את קובצי הידע הרלוונטיים..."}
                    elif name == "tool_output":
                        yield {"type": "status", "message": "מעבד את המידע שנמצא..."}
                elif event_type == "raw_response_event":
                    data = getattr(event, "data", None)
                    if getattr(data, "type", "") == "response.output_text.delta":
                        delta = getattr(data, "delta", "")
                        if delta:
                            chunks.append(delta)
                            yield {"type": "delta", "delta": delta}

            answer = "".join(chunks).strip() or str(result.final_output or "").strip()
            output = AgentAnswer(
                answer=answer,
                citations=[],
                confidence="medium",
                needs_human_review=False,
                follow_up_question=None,
            )
        except InputGuardrailTripwireTriggered as exc:
            output = AgentAnswer(
                answer="אני יכול לעזור רק בשאלות על הקורסים, השירותים והמידע המאושר של מכללת במבי.",
                citations=[],
                confidence="low",
                needs_human_review=True,
                follow_up_question="איזה קורס או נושא בבמבי מעניין אותך?",
            )
            self.db.add_message(session_id, "system", f"Input guardrail: {exc}")
        except OutputGuardrailTripwireTriggered as exc:
            output = AgentAnswer(
                answer="אין לי כרגע מידע מאושר מספיק כדי לענות בוודאות.",
                citations=[],
                confidence="low",
                needs_human_review=True,
                follow_up_question="אפשר לחדד איזה קורס או פרט אתה צריך?",
            )
            self.db.add_message(session_id, "system", f"Output guardrail: {exc}")

        self.db.add_message(session_id, "assistant", output.model_dump_json(ensure_ascii=False))
        yield {"type": "final", "response": self._client_output(output).model_dump(mode="json")}

    def _client_output(self, output: AgentAnswer) -> AgentAnswer:
        return output.model_copy(update={"citations": []})

    def _get_agent(self) -> Agent[AgentContext]:
        if self._agent is None:
            self._agent = Agent(
                name="BambiKnowledgeAgent",
                instructions=self._instructions(),
                model=self.settings.openai_model,
                output_type=AgentAnswer,
                tools=self._build_tools(),
                input_guardrails=[self._input_guardrail()],
                output_guardrails=[self._output_guardrail()],
            )
        return self._agent

    def _get_streaming_agent(self) -> Agent[AgentContext]:
        if self._streaming_agent is None:
            self._streaming_agent = Agent(
                name="BambiKnowledgeStreamingAgent",
                instructions=self._streaming_instructions(),
                model=self.settings.openai_model,
                tools=self._build_tools(),
                input_guardrails=[self._input_guardrail()],
                output_guardrails=[self._stream_output_guardrail()],
            )
        return self._streaming_agent

    def _streaming_instructions(self) -> str:
        return """
אתה צ'אטבוט של מכללת במבי - המכללה לבטיחות ונהיגה.
ענה בנימוס, בקצרה ובאותה שפה שבה נשאלת השאלה.

אם המשתמש רק מברך, אומר "שלום", "היי", "בוקר טוב" או כותב הודעה שלא מציגה צורך ברור:
ענה שאתה בוט המידע של מכללת במבי, הסבר בקצרה שאתה יכול לעזור במידע על קורסי נהיגה ותחבורה, קורסי בטיחות, תנאי קבלה, משך הקורס, מחירים, מועדים, הגעה ויצירת קשר, וסיים בשאלה "איך אפשר לעזור?"

השתמש רק בכלי הידע המקומיים שמוגדרים עבורך. כל כלי מחזיר את הטקסט המלא של קובץ ידע רלוונטי.
אל תנחש מחירים, מועדים, תנאי קבלה, קישורים, זמינות או פרטי הרשמה אם הם אינם מופיעים בכלים.
אם חסר מידע כדי לענות, שאל שאלת הבהרה קצרה או אמור שאין לך מידע מאושר מספיק.

כאשר אתה מציין מחיר, נסח באופן טבעי: "המחיר המעודכן הוא ...".
אל תכתוב "שמופיע אצלי", "לפי המידע שיש לי", "במקורות שלי" או ניסוחים דומים שחושפים את מנגנון הידע.

אין להציג למשתמש מקורות, שמות קבצים, citations, JSON או פרטי מערכת פנימיים.
החזר תשובה רגילה בלבד, בלי Markdown מורכב ובלי רשימת מקורות.
"""

    def _instructions(self) -> str:
        return """
אתה צ'אטבוט של מכללת במבי - המכללה לבטיחות ונהיגה.
תפקידך לענות בנימוס, באדיבות ובמקצועיות על שאלות לגבי הקורסים, השירותים והפעילות של המכללה.
הקפד להשיב תמיד באותה שפה שבה נשאלת השאלה.

מקצועיות ועדכניות:
מקור הידע היחיד שלך הוא כלי התוכן המקומיים שמוגדרים עבורך. כל כלי מחזיר את הטקסט המלא של קובץ ידע רלוונטי מתוך הפרויקט.
המידע בקבצים נחשב למידע העדכני והרשמי לשלב זה, כולל מסמכים עדכניים לשנת 2026 כאשר הם קיימים.
אל תנחש ואל תספק מידע שאינו נתמך בתוכן שחזר מהכלים. אם חסר פרט חשוב כדי לענות, שאל את המשתמש שאלת הבהרה קצרה.

ברכות והודעות ללא צורך ברור:
אם המשתמש רק מברך, אומר "שלום", "היי", "בוקר טוב" או כותב הודעה שלא מציגה רצון ברור, אל תענה רק בברכה קצרה.
ענה שאתה בוט המידע של מכללת במבי, הסבר בקצרה שאתה יכול לעזור במידע על קורסי נהיגה ותחבורה, קורסי בטיחות, תנאי קבלה, משך הקורס, מחירים, מועדים, הגעה ויצירת קשר, וסיים בשאלה "איך אפשר לעזור?"

ניסוח מחירים:
כאשר אתה מציין מחיר, נסח באופן טבעי: "המחיר המעודכן הוא ...".
אל תכתוב "שמופיע אצלי", "לפי המידע שיש לי", "במקורות שלי" או ניסוחים דומים שחושפים את מנגנון הידע.

היכרות עם המכללה:
בתשובות כלליות על המכללה ניתן להציג שמכללת במבי נוסדה על ידי גדעון אבירם ומכשירה נהגים, מדריכים וממוני בטיחות מזה שנים רבות.
הקמפוס הראשי שוכן באזור התעשייה כנות, רחוב אדום 34, ומתקני הלימוד בו מודרניים ומתקדמים.
השתמש בפרטים אלה רק כאשר הם מתאימים לשאלה, ובעדיפות אמת אותם מול כלי 'אודות המכללה', 'דרכי הגעה' או 'יצירת קשר'.

היצע קורסים:
המכללה מציעה קורסי נהיגה ותחבורה, כגון הוראת נהיגה, מלגזה, מכונה ניידת, רישיון משא כבד וטרקטור.
המכללה מציעה גם קורסי בטיחות, כגון מדריך עבודה בגובה, הדרכות לממוני בטיחות, השתלמויות ענפיות, קורסי עגורן ועוד.
כאשר נשאלת על קורס מסוים, השתמש בכלי המתאים ופרט רק את המידע שקיים בו: תנאי קבלה, משך, מבנה, עלויות ומועדים.

שימוש בכלים:
כדי לענות על שאלות מפורטות השתמש בכלי התוכן הנכון: כלי קורסי נהיגה, כלי קורסי בטיחות, כלי שאלות נפוצות, כלי אודות, כלי הגעה או כלי יצירת קשר.
אל תשלוף מידע שאינו קיים בכלים. אין לך גישה חופשית לאינטרנט, לאתר, ל-Google Drive או למסד נתונים סמנטי.
אין צורך להציג למשתמש מקורות, שמות קבצים או citations. השתמש בכלים כדי לוודא את התשובה, אך החזר למשתמש רק תשובה עניינית.
הכלי 'מידע על הלקוח' עדיין לא מחובר ל-myBusiness ויחזיר שלא נמצא מידע.

שירותיות:
הראה אמפתיה וסבלנות, כבד את המשתמשים וענה באופן ברור.
אם נדרשים הסברים כלליים על המכללה, דרכי הגעה או יצירת קשר, ספק אותם בהתאם למידע הזמין בכלים.

התשובה הסופית חייבת להיות JSON שתואם ל-AgentAnswer.
"""

    def _input_guardrail(self):
        @input_guardrail(name="bambi_input_guardrail", run_in_parallel=False)
        async def guardrail(_ctx, _agent, user_input):
            text = user_input if isinstance(user_input, str) else json.dumps(user_input, ensure_ascii=False)
            lowered = text.lower()
            blocked_terms = ["ignore previous", "system prompt", "api key", "secret", "password", "token"]
            off_topic_terms = ["math homework", "recipe", "bitcoin"]
            tripwire = any(term in lowered for term in blocked_terms) or any(term in lowered for term in off_topic_terms)
            return GuardrailFunctionOutput(output_info={"matched": tripwire}, tripwire_triggered=tripwire)

        return guardrail

    def _output_guardrail(self):
        @output_guardrail(name="bambi_output_guardrail")
        async def guardrail(_ctx, _agent, output: AgentAnswer):
            unsupported_claim = any(token in output.answer for token in ["לדעתי", "נראה לי", "כנראה"])
            internal_wording = any(
                token in output.answer
                for token in ["שמופיע אצלי", "לפי המידע שיש לי", "במקורות שלי", "בכלים שלי"]
            )
            return GuardrailFunctionOutput(
                output_info={"unsupported_claim": unsupported_claim, "internal_wording": internal_wording},
                tripwire_triggered=unsupported_claim or internal_wording,
            )

        return guardrail

    def _stream_output_guardrail(self):
        @output_guardrail(name="bambi_stream_output_guardrail")
        async def guardrail(_ctx, _agent, output: str):
            lowered = str(output).lower()
            unsupported_claim = any(token in str(output) for token in ["לדעתי", "נראה לי", "כנראה"])
            leaks_sources = any(token in lowered for token in ["citation", "source_id", "locator", "tool output"])
            internal_wording = any(
                token in str(output)
                for token in ["שמופיע אצלי", "לפי המידע שיש לי", "במקורות שלי", "בכלים שלי"]
            )
            return GuardrailFunctionOutput(
                output_info={
                    "unsupported_claim": unsupported_claim,
                    "leaks_sources": leaks_sources,
                    "internal_wording": internal_wording,
                },
                tripwire_triggered=unsupported_claim or leaks_sources or internal_wording,
            )

        return guardrail

    def _read_knowledge_tool(self, tool_id: str) -> dict[str, Any]:
        payload = self.knowledge_files.read_tool_file(tool_id)
        self.db.log_tool_call(None, tool_id, {}, payload, bool(payload["found"]))
        return payload

    def _build_tools(self) -> list[Any]:
        service = self
        tools: list[Any] = []

        for spec in service.knowledge_files.tool_specs():
            if spec.tool_id == "customer_info":

                async def customer_info(identifier: str | None = None) -> dict[str, Any]:
                    """Customer information tool. Placeholder for myBusiness; currently returns no local match."""
                    payload = service.knowledge_files.read_tool_file("customer_info")
                    payload["identifier"] = identifier
                    service.db.log_tool_call(None, "customer_info", {"identifier": identifier}, payload, False)
                    return payload

                tools.append(
                    function_tool(
                        customer_info,
                        name_override=spec.tool_id,
                        description_override=spec.description,
                    )
                )
                continue

            def build_reader(tool_id: str):
                async def read_knowledge_file() -> dict[str, Any]:
                    return service._read_knowledge_tool(tool_id)

                return read_knowledge_file

            read_knowledge_file = build_reader(spec.tool_id)

            read_knowledge_file.__name__ = spec.tool_id
            tools.append(
                function_tool(
                    read_knowledge_file,
                    name_override=spec.tool_id,
                    description_override=spec.description,
                )
            )

        return tools
