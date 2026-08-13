import frappe

INTERVIEW_TYPE = "AyP - Entrevista estructurada"
QUESTIONS = """1. ¿Qué te interesa de esta vacante y qué entendiste de sus responsabilidades?
2. Cuéntame una experiencia concreta parecida al trabajo del puesto. ¿Cuál fue tu responsabilidad y el resultado?
3. Describe una situación difícil con un cliente o compañero. ¿Qué hiciste y qué aprendiste?
4. Cuando tienes varias tareas urgentes, ¿cómo decides el orden y cómo comunicas los retrasos?
5. Cuéntame algo que tuviste que aprender rápido para resolver un problema real.
6. Describe un error de trabajo que hayas cometido. ¿Cómo lo detectaste, corregiste y evitaste repetirlo?
7. Presenta un caso práctico directamente relacionado con la vacante y pide que explique su razonamiento paso a paso.
8. Confirma disponibilidad, horario, ubicación y condiciones ya informadas para la vacante.
9. ¿Qué apoyo o herramientas necesitarías para rendir bien durante tus primeros 30 días?
10. ¿Qué preguntas tienes sobre el puesto, el equipo o la forma de trabajo?

Regla de evidencia: registrar ejemplos observables y respuestas concretas; no inferir edad, salud, estado familiar, religión, origen u otras características protegidas."""

SKILLS = (
	("AyP - Cumplimiento de requisitos", "Demuestra los requisitos indispensables de la vacante con evidencia verificable."),
	("AyP - Experiencia y conocimiento del rol", "Conecta experiencias concretas con las responsabilidades reales del puesto."),
	("AyP - Servicio y comunicación", "Comunica con claridad y demuestra orientación de servicio y colaboración."),
	("AyP - Resolución de situaciones", "Analiza casos, prioriza y explica decisiones de forma estructurada."),
	("AyP - Motivación y disponibilidad", "Muestra interés informado y compatibilidad con condiciones ya publicadas."),
)


def execute():
	for skill_name, description in SKILLS:
		if not frappe.db.exists("Skill", skill_name):
			frappe.get_doc(
				{"doctype": "Skill", "skill_name": skill_name, "description": description}
			).insert(ignore_permissions=True)

	if frappe.db.exists("Interview Type", INTERVIEW_TYPE):
		doc = frappe.get_doc("Interview Type", INTERVIEW_TYPE)
		changed = False
		if not doc.custom_ayp_structured_questions:
			doc.custom_ayp_structured_questions = QUESTIONS
			changed = True
		if not doc.expected_skill_set:
			for skill_name, _description in SKILLS:
				doc.append("expected_skill_set", {"skill": skill_name})
			changed = True
		if changed:
			doc.save(ignore_permissions=True)
		return

	frappe.get_doc(
		{
			"doctype": "Interview Type",
			"interview_type_name": INTERVIEW_TYPE,
			"description": "Entrevista estructurada AyP v1. La recomendación es asistida y la decisión final es humana.",
			"expected_average_rating": 0.7,
			"custom_ayp_structured_questions": QUESTIONS,
			"expected_skill_set": [{"skill": skill_name} for skill_name, _description in SKILLS],
		}
	).insert(ignore_permissions=True)
