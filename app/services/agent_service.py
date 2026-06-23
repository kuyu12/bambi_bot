from __future__ import annotations

import json
import re
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
from app.services.contact_channels import ContactChannelService
from app.services.knowledge_files import KnowledgeFileService
from app.services.mybusiness import MyBusinessService
from app.services.payment_links import PaymentLinkService


@dataclass
class AgentContext:
    session_id: str


class AgentService:
    def __init__(self, settings: Settings, db: Database, knowledge_files: KnowledgeFileService):
        self.settings = settings
        self.db = db
        self.knowledge_files = knowledge_files
        self.mybusiness = MyBusinessService(settings)
        self.payment_links = PaymentLinkService()
        self.contact_channels = ContactChannelService()
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
                instructions=self._instructions() + self._mybusiness_instructions() + self._sales_flow_instructions(),
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
                instructions=self._streaming_instructions() + self._mybusiness_instructions() + self._sales_flow_instructions(),
                model=self.settings.openai_model,
                tools=self._build_tools(),
                input_guardrails=[self._input_guardrail()],
                output_guardrails=[self._stream_output_guardrail()],
            )
        return self._streaming_agent

    def _streaming_instructions(self) -> str:
        return """
אתה צ'אטבוט של מכללת במבי - המכללה לבטיחות ונהיגה.
תפקידך לענות בנימוס, באדיבות ובמקצועיות על שאלות לגבי הקורסים, השירותים והפעילות של המכללה ולרשום לקוחות לקורסים אם הם רוצים.
הקפד להשיב תמיד באותה שפה שבה נשאלת השאלה.

מקצועיות ועדכניות:
מקור הידע היחיד שלך הוא כלי התוכן המקומיים שמוגדרים עבורך. כל כלי מחזיר את הטקסט הרלוונטי של התוכן.
המידע בקבצים נחשב למידע העדכני והרשמי, אל תנחש ואל תספק מידע שאינו נתמך בתוכן שחזר מהכלים. אם חסר פרט חשוב כדי לענות, שאל את המשתמש שאלת הבהרה קצרה.

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
שים לב שאתה מוודא שאתם מדברים על הקורס הנכון, יש הרבה בלבול לפעמים בין ריענון לקורס עצמו (לדוגמה קורס ריענון מלגזה וקורס מלגזה זה דברים שונים, יש לוודא עם הלקוח על איזה קורס הוא מדבר)
הבחנת קורסים:
אל תציע קורס, מועד או לינק תשלום אם אינך בטוח שזה הקורס המדויק שהמשתמש התכוון אליו. אם יש כמה אפשרויות קרובות, שאל שאלת הבהרה קצרה לפני שימוש בכלי תשלום או הרשמה.
הבדלים שחובה לשמור עליהם: קורס רגיל שונה מריענון; קורס תלמיד שונה מקורס מדריכים; ריענון הובלת/מוביל חומ"ס שונה מריענון אחראי שינוע חומ"ס; קורס חומ"ס שונה מריענון חומ"ס; קורס מלגזה שונה מריענון מלגזה ומריענון מדריך מלגזה; קורס מכונה ניידת שונה מקורס מדריכים למכונה ניידת; רישיון מוביל קצר/רגיל שונה מרישיון מוביל ארוך אם המידע אינו מאשר שזה אותו מסלול.
כאשר המשתמש אומר רק שם כללי כמו "חומס", "מלגזה", "מכונה ניידת", "מנוף", "עגורן" או "מוביל", שאל למה בדיוק הוא מתכוון במקום לבחור לבד.
הרשמה:
כאשר לקוח רוצה להירשם, אל תפנה אותו להרשמה בטלפון, בוואטסאפ או ביצירת קשר כללית כל עוד אפשר לבצע את התהליך בצ'אט.
הסבר שאתה יכול לעזור בתהליך ההרשמה כאן, ולאחר מכן פעל לפי תהליך המכירה וההרשמה המחייב: בדיקת תנאי הקורס, הצגת מועדים מהכלים, בחירת מועד, בקשת פרטים, שליחת לינק תשלום מאושר, אימות תשלום מול המערכת ורק אז שימוש בכלי ההרשמה.
מותר להפנות לנציג או לטלפון רק כאשר אחד הכלים מחזיר חסימה, חסר מידע שלא ניתן להשלים בצ'אט, אין לינק תשלום מאושר, אין דרך לאמת תשלום, אין לקוח קיים במערכת או שהמשתמש מבקש במפורש לדבר עם נציג.
מעבר למענה אנושי:
במקרה של תוכן שלא קיים לך, או צורך או בקשה להעביר למענה אנושי, אם השיחה עוסקת בקורס ספציפי חובה לקרוא קודם ל-get_course_contact_channel עם שם הקורס ולתת את מספר הוואטסאפ הייעודי למשפחת הקורס.
מספר המשרד הכללי מיועד רק לשאלות כלליות או כאשר get_course_contact_channel לא מצא משפחת קורס ברורה.

שימוש בכלים:
כדי לענות על שאלות מפורטות השתמש בכלי התוכן הנכון: כלי קורסי נהיגה, כלי קורסי בטיחות, כלי שאלות נפוצות, כלי אודות, כלי הגעה או כלי יצירת קשר.
אל תשלוף מידע שאינו קיים בכלים.
אין צורך להציג למשתמש מקורות, שמות קבצים או citations. השתמש בכלים כדי לוודא את התשובה, אך החזר למשתמש רק תשובה עניינית.

שירותיות:
הראה אמפתיה וסבלנות, כבד את המשתמשים וענה באופן ברור.
אם נדרשים הסברים כלליים על המכללה, דרכי הגעה או יצירת קשר, ספק אותם בהתאם למידע הזמין בכלים.

אין להציג למשתמש מקורות, שמות קבצים, citations, JSON או פרטי מערכת פנימיים.
החזר תשובה רגילה בלבד, בלי Markdown מורכב ובלי רשימת מקורות.
"""

    def _instructions(self) -> str:
        return """
אתה צ'אטבוט של מכללת במבי - המכללה לבטיחות ונהיגה.
תפקידך לענות בנימוס, באדיבות ובמקצועיות על שאלות לגבי הקורסים, השירותים והפעילות של המכללה ולרשום לקוחות לקורסים אם הם רוצים.
הקפד להשיב תמיד באותה שפה שבה נשאלת השאלה.

מקצועיות ועדכניות:
מקור הידע היחיד שלך הוא כלי התוכן המקומיים שמוגדרים עבורך. כל כלי מחזיר את הטקסט הרלוונטי של התוכן.
המידע בקבצים נחשב למידע העדכני והרשמי, אל תנחש ואל תספק מידע שאינו נתמך בתוכן שחזר מהכלים. אם חסר פרט חשוב כדי לענות, שאל את המשתמש שאלת הבהרה קצרה.

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
שים לב שאתה מוודא שאתם מדברים על הקורס הנכון, יש הרבה בלבול לפעמים בין ריענון לקורס עצמו (לדוגמה קורס ריענון מלגזה וקורס מלגזה זה דברים שונים, יש לוודא עם הלקוח על איזה קורס הוא מדבר)
הבחנת קורסים:
אל תציע קורס, מועד או לינק תשלום אם אינך בטוח שזה הקורס המדויק שהמשתמש התכוון אליו. אם יש כמה אפשרויות קרובות, שאל שאלת הבהרה קצרה לפני שימוש בכלי תשלום או הרשמה.
הבדלים שחובה לשמור עליהם: קורס רגיל שונה מריענון; קורס תלמיד שונה מקורס מדריכים; ריענון הובלת/מוביל חומ"ס שונה מריענון אחראי שינוע חומ"ס; קורס חומ"ס שונה מריענון חומ"ס; קורס מלגזה שונה מריענון מלגזה ומריענון מדריך מלגזה; קורס מכונה ניידת שונה מקורס מדריכים למכונה ניידת; רישיון מוביל קצר/רגיל שונה מרישיון מוביל ארוך אם המידע אינו מאשר שזה אותו מסלול.
כאשר המשתמש אומר רק שם כללי כמו "חומס", "מלגזה", "מכונה ניידת", "מנוף", "עגורן" או "מוביל", שאל למה בדיוק הוא מתכוון במקום לבחור לבד.
הרשמה:
כאשר לקוח רוצה להירשם, אל תפנה אותו להרשמה בטלפון, בוואטסאפ או ביצירת קשר כללית כל עוד אפשר לבצע את התהליך בצ'אט.
הסבר שאתה יכול לעזור בתהליך ההרשמה כאן, ולאחר מכן פעל לפי תהליך המכירה וההרשמה המחייב: בדיקת תנאי הקורס, הצגת מועדים מהכלים, בחירת מועד, בקשת פרטים, שליחת לינק תשלום מאושר, אימות תשלום מול המערכת ורק אז שימוש בכלי ההרשמה.
מותר להפנות לנציג או לטלפון רק כאשר אחד הכלים מחזיר חסימה, חסר מידע שלא ניתן להשלים בצ'אט, אין לינק תשלום מאושר, אין דרך לאמת תשלום, אין לקוח קיים במערכת או שהמשתמש מבקש במפורש לדבר עם נציג.
מעבר למענה אנושי:
במקרה של תוכן שלא קיים לך, או צורך או בקשה להעביר למענה אנושי, אם השיחה עוסקת בקורס ספציפי חובה לקרוא קודם ל-get_course_contact_channel עם שם הקורס ולתת את מספר הוואטסאפ הייעודי למשפחת הקורס.
מספר המשרד הכללי מיועד רק לשאלות כלליות או כאשר get_course_contact_channel לא מצא משפחת קורס ברורה.

שימוש בכלים:
כדי לענות על שאלות מפורטות השתמש בכלי התוכן הנכון: כלי קורסי נהיגה, כלי קורסי בטיחות, כלי שאלות נפוצות, כלי אודות, כלי הגעה או כלי יצירת קשר.
אל תשלוף מידע שאינו קיים בכלים.
אין צורך להציג למשתמש מקורות, שמות קבצים או citations. השתמש בכלים כדי לוודא את התשובה, אך החזר למשתמש רק תשובה עניינית.

שירותיות:
הראה אמפתיה וסבלנות, כבד את המשתמשים וענה באופן ברור.
אם נדרשים הסברים כלליים על המכללה, דרכי הגעה או יצירת קשר, ספק אותם בהתאם למידע הזמין בכלים.

התשובה הסופית חייבת להיות JSON שתואם ל-AgentAnswer.
"""

    def _mybusiness_instructions(self) -> str:
        return """

כלי MyBusiness:
יש לך גישה לכלי MyBusiness לבדיקת לקוחות קיימים, קטגוריות קורסים, מועדי קורסים פתוחים, בדיקת זכאות לרישום, ורישום לקוח קיים לקורס.
רוב הכלים הם לקריאה בלבד. הכלי היחיד שמותר לו ליצור רשומה הוא register_customer_to_course, והוא יוצר CourseEnrollment רק אחרי בדיקת זכאות מלאה.
אל תיצור לקוחות, אל תעדכן לקוחות, אל תעדכן קורסים ואל תשתמש בכלי כללי לשינוי נתונים.

כאשר משתמש שואל על מועדי קורסים או זמינות:
1. קרא קודם ל-list_course_categories עם שם הקורס שהמשתמש ביקש.
2. אם נמצאה קטגוריה אחת, השתמש ב-category_id שלה.
3. אם נמצאו כמה קטגוריות, שאל שאלת הבהרה קצרה.
4. קרא ל-find_available_course_dates עם category_id.
5. הצג רק מועדים שהכלי החזיר, כולל מספר מקומות פנויים.
6. אל תנחש מועדים, מחירים, מיקום או זמינות.
7. אם לא נמצאו מועדים פתוחים עם מקומות פנויים, אמור זאת בצורה ברורה.

כאשר משתמש מבקש לבדוק אם לקוח קיים:
1. אם חסר טלפון או מספר מזהה, בקש אותו.
2. קרא ל-find_existing_customer.
3. אם נמצא לקוח אחד, סכם בקצרה רק פרטים נחוצים לזיהוי.
4. אם נמצאו כמה לקוחות, בקש מהמשתמש לבחור.
5. אל תציג מידע אישי שאינו נחוץ.

כאשר משתמש מבקש להירשם לקורס:
1. ודא שיש לקוח קיים ב-MyBusiness באמצעות find_existing_customer. אם אין לקוח קיים, בקש מעבר למענה אנושי.
2. ודא שהקורס והמועד המבוקש ברורים. עבור מועדים השתמש ב-list_course_categories ואז find_available_course_dates.
3. לפני רישום, השתמש ב-check_customer_registration_eligibility עם account_id ו-course_id. אם יש sale_id, העבר גם אותו.
4. אם חסר sale_id או payment_status, בקש אותם או הסבר שנדרש טיפול אנושי. אל תנחש sale_id ואל תנחש סטטוס תשלום.
5. payment_status חייב להיות אחד מהערכים: PAID, PARTIAL, UNPAID, COMPANY_INVOICE.
6. ברירת המחדל לרישום היא dry_run=true. הצג למפעיל שהבדיקה עברה ומה היה נרשם. בצע dry_run=false רק אם המשתמש ביקש במפורש לבצע רישום אמיתי ויש את כל הפרטים.
7. אם הכלי מחזיר blocking_reasons, אל תעקוף אותם ואל תנסה להירשם שוב ללא שינוי בפרטים.
8. לעולם אל תציג למשתמש payload פנימי, מזהי מערכת לא נחוצים, מפתחות API או פלט גולמי של הכלים.
"""

    def _sales_flow_instructions(self) -> str:
        return """

תהליך מכירה והרשמה מחייב:
כאשר לקוח רוצה להירשם לקורס, פעל אך ורק לפי הסדר הבא. אל תדלג על שלבים ואל תרשום לקוח לפני תשלום מאומת.
אם כלי ידע כלשהו כולל טקסט ישן כמו "להרשמה התקשרו", "פנו בוואטסאפ", "צרו קשר להרשמה" או ניסוח דומה, אל תציג אותו כהנחיית הרשמה ואל תפנה את הלקוח לשם כברירת מחדל.
המידע בקבצי הידע משמש להבנת הקורס בלבד. תהליך הרשמה, תשלום ובחירת מועדים מתבצע רק דרך כלי MyBusiness וכלי התשלום שהוגדרו לך.
הפניה לטלפון, וואטסאפ או נציג מותרת רק כחריג: חסימה מהכלים, חוסר מידע קריטי, תשלום שלא ניתן לאמת, לקוח שלא נמצא במערכת, קורס ללא לינק תשלום מאושר, או בקשה מפורשת של המשתמש לדבר עם נציג.
לפני תחילת התהליך ודא ששם הקורס חד-משמעי. אם המשתמש ביקש קורס ממשפחה שיש בה כמה מסלולים דומים, עצור ושאל הבהרה. אין להשתמש בלינק תשלום או במועד של מסלול דומה.
כאשר צריך להפנות לנציג לגבי קורס ספציפי, אל תשתמש אוטומטית בטלפון המשרד. קרא ל-get_course_contact_channel והחזר את מספר הוואטסאפ הייעודי שהכלי מצא. אם הכלי מחזיר כמה התאמות, שאל לאיזה קורס/משפחה הכוונה. אם אין התאמה, רק אז השתמש בפרטי המשרד הכלליים מכלי יצירת הקשר.

1. בדיקת דרישות הקורס:
השתמש בכלי הידע של הקורס כדי לבדוק תנאי קבלה, מסמכים נדרשים, גיל, רישיון קודם, טופס ירוק, בדיקות או כל דרישה אחרת.
אם קיימות דרישות, שאל את הלקוח עליהן וקבל ממנו אישור ברור שהוא עומד בהן ומכיר אותן.
אם הלקוח לא עומד בדרישות, לא בטוח, או חסר מידע חשוב - עצור והעבר לנציג.

2. בחירת מועד:
השתמש ב-list_course_categories ואז find_available_course_dates כדי להציג את המועדים הקרובים עם מקומות פנויים.
שאל את הלקוח איזה מועד מתאים לו. אל תמשיך לתשלום בלי מועד מוסכם כאשר הקורס דורש בחירת מועד.

3. פרטים ולינק תשלום:
לאחר שיש קורס ומועד מוסכמים, קרא ל-get_course_payment_instructions עם שם הקורס.
בקש מהלקוח את כל הפרטים שהכלי החזיר כ-required_customer_details.
שלח ללקוח את לינק התשלום שהכלי החזיר, יחד עם הערת התשלום אם קיימת.
אם אין לינק תשלום לקורס או שיש התאמה לא חד-משמעית, אל תמציא לינק ואל תשתמש בלינק של קורס אחר - העבר לנציג.

4. לאחר תשלום:
בקש מהלקוח לעדכן כשהתשלום הסתיים. אם הלקוח אומר שהוא שילם, חובה לבדוק מול MyBusiness לפני רישום.
אין להסתמך רק על אמירה של הלקוח או צילום מסך כראיית תשלום סופית.
אם אין דרך לאמת במערכת שהתשלום בוצע או שאין sale_id מתאים שמשויך ללקוח, העבר לנציג.

5. רישום לקורס:
רק לאחר שיש לקוח קיים, קורס ומועד מוסכמים, עמידה בדרישות, תשלום מאומת, sale_id מתאים ו-payment_status מפורש - השתמש ב-check_customer_registration_eligibility.
אם הזכאות תקינה, השתמש ב-register_customer_to_course.
ברירת המחדל היא dry_run=true. dry_run=false מותר רק אם התשלום אומת ויש בקשה מפורשת לבצע רישום אמיתי.

6. טיפול בבעיות:
אם יש blocking_reasons, חוסר התאמה, קורס מלא, קורס לא פתוח, לקוח שכבר רשום למועד עתידי, בעיית תשלום או חוסר פרטים - אל תנסה לעקוף. העבר לנציג וסכם בקצרה את מצב השיחה.
אל תציג למשתמש JSON, payload פנימי, מזהי מערכת לא נחוצים, פלט גולמי של כלים או מקורות.
"""

    def _input_guardrail(self):
        service = self

        @input_guardrail(name="bambi_input_guardrail", run_in_parallel=False)
        async def guardrail(_ctx, _agent, user_input):
            text = user_input if isinstance(user_input, str) else json.dumps(user_input, ensure_ascii=False)
            lowered = service._normalize_security_text(text)
            blocked_terms = [
                "ignore previous",
                "ignore all previous",
                "forget previous",
                "disregard instructions",
                "developer message",
                "system prompt",
                "reveal prompt",
                "show prompt",
                "api key",
                "secret",
                "password",
                "token",
                "master key",
                "bypass",
                "override policy",
                "do not use tools",
                "dont use tools",
                "skip validation",
                "skip eligibility",
                "register without payment",
                "fake payment",
                "invent payment link",
                "use another payment link",
                "תתעלם מההוראות",
                "התעלם מההוראות",
                "תעקוף",
                "עקוף",
                "בלי להשתמש בכלים",
                "אל תשתמש בכלים",
                "תמציא לינק",
                "לינק אחר",
                "בלי לשלם",
                "תרשום בלי תשלום",
                "דלג על הבדיקה",
                "תדלג על הבדיקה",
                "הצג את הפרומפט",
                "סיסטם פרומפט",
                "מפתח api",
                "סיסמה",
            ]
            off_topic_terms = ["math homework", "recipe", "bitcoin"]
            tripwire = any(term in lowered for term in blocked_terms) or any(term in lowered for term in off_topic_terms)
            return GuardrailFunctionOutput(output_info={"matched": tripwire}, tripwire_triggered=tripwire)

        return guardrail

    def _output_guardrail(self):
        service = self

        @output_guardrail(name="bambi_output_guardrail")
        async def guardrail(_ctx, _agent, output: AgentAnswer):
            unsupported_claim = any(token in output.answer for token in ["לדעתי", "נראה לי", "כנראה"])
            internal_wording = any(
                token in output.answer
                for token in ["שמופיע אצלי", "לפי המידע שיש לי", "במקורות שלי", "בכלים שלי"]
            )
            unapproved_payment_url = service._has_unapproved_payment_url(output.answer)
            return GuardrailFunctionOutput(
                output_info={
                    "unsupported_claim": unsupported_claim,
                    "internal_wording": internal_wording,
                    "unapproved_payment_url": unapproved_payment_url,
                },
                tripwire_triggered=unsupported_claim or internal_wording or unapproved_payment_url,
            )

        return guardrail

    def _stream_output_guardrail(self):
        service = self

        @output_guardrail(name="bambi_stream_output_guardrail")
        async def guardrail(_ctx, _agent, output: str):
            lowered = str(output).lower()
            unsupported_claim = any(token in str(output) for token in ["לדעתי", "נראה לי", "כנראה"])
            leaks_sources = any(token in lowered for token in ["citation", "source_id", "locator", "tool output"])
            internal_wording = any(
                token in str(output)
                for token in ["שמופיע אצלי", "לפי המידע שיש לי", "במקורות שלי", "בכלים שלי"]
            )
            unapproved_payment_url = service._has_unapproved_payment_url(str(output))
            return GuardrailFunctionOutput(
                output_info={
                    "unsupported_claim": unsupported_claim,
                    "leaks_sources": leaks_sources,
                    "internal_wording": internal_wording,
                    "unapproved_payment_url": unapproved_payment_url,
                },
                tripwire_triggered=unsupported_claim or leaks_sources or internal_wording or unapproved_payment_url,
            )

        return guardrail

    def _normalize_security_text(self, text: str) -> str:
        return re.sub(r"\s+", " ", text.lower().replace("׳", "'").replace("״", '"')).strip()

    def _has_unapproved_payment_url(self, text: str) -> bool:
        urls = re.findall(r"https?://[^\s)>\]\"']+", text)
        if not urls:
            return False

        allowed_urls = self.payment_links.allowed_payment_urls()
        for url in urls:
            cleaned = url.rstrip(".,;:!?")
            is_payment_url = "payment-btn-page" in cleaned or "mybooks" in cleaned or cleaned == "https://tinyurl.com/3zdk77d6"
            if is_payment_url and cleaned not in allowed_urls:
                return True
        return False

    def _read_knowledge_tool(self, tool_id: str) -> dict[str, Any]:
        payload = self.knowledge_files.read_tool_file(tool_id)
        self.db.log_tool_call(None, tool_id, {}, payload, bool(payload["found"]))
        return payload

    def _build_tools(self) -> list[Any]:
        service = self
        tools: list[Any] = []

        async def find_existing_customer(identifier: str) -> dict[str, Any]:
            """Find an existing customer/student in MyBusiness by phone number, Israeli ID, or company ID. Read-only."""
            try:
                payload = await service.mybusiness.find_existing_customer(identifier)
            except Exception as exc:  # noqa: BLE001 - tool should return structured failure to the agent.
                payload = {"found": False, "match_count": 0, "returned_count": 0, "customers": [], "error": type(exc).__name__}
            service.db.log_tool_call(
                None,
                "find_existing_customer",
                {"identifier_provided": bool(identifier)},
                {
                    "found": payload.get("found"),
                    "match_count": payload.get("match_count"),
                    "returned_count": payload.get("returned_count"),
                    "error": payload.get("error"),
                    "message": payload.get("message"),
                },
                bool(payload.get("found")),
            )
            return payload

        async def list_course_categories(search: str | None = None) -> dict[str, Any]:
            """List available MyBusiness course categories dynamically from ProductCategories. Read-only."""
            try:
                payload = await service.mybusiness.list_course_categories(search)
            except Exception as exc:  # noqa: BLE001 - tool should return structured failure to the agent.
                payload = {"categories_count": 0, "categories": [], "error": type(exc).__name__}
            service.db.log_tool_call(
                None,
                "list_course_categories",
                {"search": search},
                {
                    "categories_count": payload.get("categories_count"),
                    "error": payload.get("error"),
                    "message": payload.get("message"),
                },
                bool(payload.get("categories_count")),
            )
            return payload

        async def find_available_course_dates(
            category_id: str | None = None,
            category_code: str | None = None,
            category_name: str | None = None,
        ) -> dict[str, Any]:
            """Find future open course instances by MyBusiness category, returning only courses with available seats. Read-only."""
            try:
                payload = await service.mybusiness.find_available_course_dates(category_id, category_code, category_name)
            except Exception as exc:  # noqa: BLE001 - tool should return structured failure to the agent.
                payload = {"found": False, "available_courses_count": 0, "courses": [], "error": type(exc).__name__}
            service.db.log_tool_call(
                None,
                "find_available_course_dates",
                {"category_id": category_id, "category_code": category_code, "category_name": category_name},
                {
                    "found": payload.get("found"),
                    "available_courses_count": payload.get("available_courses_count"),
                    "raw_matching_courses_before_capacity_filter": payload.get("raw_matching_courses_before_capacity_filter"),
                    "ambiguous": payload.get("ambiguous"),
                    "error": payload.get("error"),
                    "message": payload.get("message"),
                },
                bool(payload.get("found")),
            )
            return payload

        async def check_customer_registration_eligibility(
            account_id: str,
            course_id: str,
            sale_id: str | None = None,
            allow_tentative_courses: bool = False,
        ) -> dict[str, Any]:
            """Check if an existing MyBusiness customer can be registered to a specific course. Read-only."""
            try:
                payload = await service.mybusiness.check_customer_registration_eligibility(
                    account_id=account_id,
                    course_id=course_id,
                    sale_id=sale_id,
                    allow_tentative_courses=allow_tentative_courses,
                )
            except Exception as exc:  # noqa: BLE001 - tool should return structured failure to the agent.
                payload = {"can_register": False, "blocking_reasons": [type(exc).__name__]}
            service.db.log_tool_call(
                None,
                "check_customer_registration_eligibility",
                {"account_id": account_id, "course_id": course_id, "sale_id": sale_id, "allow_tentative_courses": allow_tentative_courses},
                {
                    "can_register": payload.get("can_register"),
                    "blocking_reasons": payload.get("blocking_reasons"),
                    "existing_future_active_enrollments_count": len(payload.get("existing_future_active_enrollments") or []),
                },
                bool(payload.get("can_register")),
            )
            return payload

        async def register_customer_to_course(
            account_id: str,
            course_id: str,
            sale_id: str,
            payment_status: str,
            amount_paid: float = 0,
            comment: str | None = None,
            allow_tentative_courses: bool = False,
            dry_run: bool = True,
        ) -> dict[str, Any]:
            """Register an existing MyBusiness customer to a course after full eligibility checks. Defaults to dry_run."""
            try:
                payload = await service.mybusiness.register_customer_to_course(
                    account_id=account_id,
                    course_id=course_id,
                    sale_id=sale_id,
                    payment_status=payment_status,
                    amount_paid=amount_paid,
                    comment=comment,
                    allow_tentative_courses=allow_tentative_courses,
                    dry_run=dry_run,
                )
            except Exception as exc:  # noqa: BLE001 - tool should return structured failure to the agent.
                payload = {
                    "created": False,
                    "dry_run": dry_run,
                    "eligibility": {"can_register": False, "blocking_reasons": [type(exc).__name__]},
                }
            service.db.log_tool_call(
                None,
                "register_customer_to_course",
                {
                    "account_id": account_id,
                    "course_id": course_id,
                    "sale_id": sale_id,
                    "payment_status": payment_status,
                    "amount_paid": amount_paid,
                    "comment_provided": bool(comment),
                    "allow_tentative_courses": allow_tentative_courses,
                    "dry_run": dry_run,
                },
                {
                    "created": payload.get("created"),
                    "dry_run": payload.get("dry_run"),
                    "eligibility": payload.get("eligibility"),
                },
                bool(payload.get("created") or payload.get("dry_run")),
            )
            return payload

        async def get_course_payment_instructions(course_name: str) -> dict[str, Any]:
            """Return required registration details and the approved payment link for a specific Bambi course."""
            try:
                payload = service.payment_links.find_payment_instructions(course_name)
            except Exception as exc:  # noqa: BLE001 - tool should return structured failure to the agent.
                payload = {"found": False, "matches_count": 0, "matches": [], "error": type(exc).__name__}
            course = payload.get("course") or {}
            service.db.log_tool_call(
                None,
                "get_course_payment_instructions",
                {"course_name": course_name},
                {
                    "found": payload.get("found"),
                    "ambiguous": payload.get("ambiguous"),
                    "matches_count": payload.get("matches_count"),
                    "course_key": course.get("course_key"),
                    "has_payment_link": bool(course.get("payment_link")),
                    "error": payload.get("error"),
                    "message": payload.get("message"),
                },
                bool(payload.get("found")),
            )
            return payload

        async def get_course_contact_channel(course_name: str) -> dict[str, Any]:
            """Return the course-family-specific Bambi WhatsApp contact for human handoff."""
            try:
                payload = service.contact_channels.find_course_contact(course_name)
            except Exception as exc:  # noqa: BLE001 - tool should return structured failure to the agent.
                payload = {
                    "found": False,
                    "ambiguous": False,
                    "matches_count": 0,
                    "matches": [],
                    "error": type(exc).__name__,
                }
            contact = payload.get("contact") or {}
            service.db.log_tool_call(
                None,
                "get_course_contact_channel",
                {"course_name": course_name},
                {
                    "found": payload.get("found"),
                    "ambiguous": payload.get("ambiguous"),
                    "matches_count": payload.get("matches_count"),
                    "owner": contact.get("owner"),
                    "family": contact.get("family"),
                    "error": payload.get("error"),
                    "message": payload.get("message"),
                },
                bool(payload.get("found")),
            )
            return payload

        tools.extend(
            [
                function_tool(find_existing_customer),
                function_tool(list_course_categories),
                function_tool(find_available_course_dates),
                function_tool(check_customer_registration_eligibility),
                function_tool(register_customer_to_course),
                function_tool(get_course_payment_instructions),
                function_tool(get_course_contact_channel),
            ]
        )

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
