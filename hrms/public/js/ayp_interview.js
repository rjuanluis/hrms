frappe.ui.form.on("Interview", {
	refresh(frm) {
		frm.trigger("render_ayp_interview_kit");
	},

	interview_type(frm) {
		frm.trigger("render_ayp_interview_kit");
	},

	async render_ayp_interview_kit(frm) {
		const wrapper = frm.fields_dict.custom_ayp_interview_kit?.wrapper;
		if (!wrapper) return;
		let questions = frm.doc.custom_ayp_questions_snapshot || "";
		if (!questions && frm.doc.interview_type) {
			const response = await frappe.db.get_value(
				"Interview Type",
				frm.doc.interview_type,
				"custom_ayp_structured_questions",
			);
			questions = response?.message?.custom_ayp_structured_questions || "";
		}
		const escape = frappe.utils.escape_html;
		const lines = questions
			.split("\n")
			.map((line) => line.trim())
			.filter(Boolean);
		const body = lines.length
			? `<ol class="ayp-interview-questions">${lines
					.filter((line) => /^\d+\./.test(line))
					.map((line) => `<li>${escape(line.replace(/^\d+\.\s*/, ""))}</li>`)
					.join("")}</ol><p class="text-muted">${escape(
					lines.find((line) => line.startsWith("Regla de evidencia:")) || "",
			  )}</p>`
			: `<p class="text-muted">${__(
					"Este tipo de entrevista no tiene preguntas estructuradas configuradas.",
			  )}</p>`;
		$(wrapper).html(
			`<div class="ayp-interview-kit"><p><strong>${__(
				"Usa las mismas preguntas y registra evidencia observable para cada persona.",
			)}</strong></p>${body}</div>`,
		);
	},
});
