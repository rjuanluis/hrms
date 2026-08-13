frappe.pages["ayp-candidate-review"].on_page_load = function (wrapper) {
	wrapper.candidate_review = new AYPCandidateReview(wrapper);
};

frappe.pages["ayp-candidate-review"].on_page_show = function (wrapper) {
	if (!wrapper.candidate_review.loading) {
		wrapper.candidate_review.load_candidates({ reset: true });
		wrapper.candidate_review.load_active_filtered_run();
	}
};

class AYPCandidateReview {
	constructor(wrapper) {
		this.page = frappe.ui.make_app_page({
			parent: wrapper,
			title: __("Candidate Review"),
			single_column: true,
		});
		this.page_length = 50;
		this.max_batch_size = 100;
		this.start = 0;
		this.rows = [];
		this.selected = new Set();
		this.has_more = false;
		this.total_count = 0;
		this.loading = false;
		this.loaded = false;
		this.filter_timer = null;
		this.load_generation = 0;
		this.active_filtered_run = null;
		this.make_layout();
		this.make_filters();
		this.make_actions();
		this.bind_events();
	}

	make_layout() {
		this.$root = $(
			`<section class="ayp-candidate-review" aria-labelledby="candidate-review-heading">
				<div class="review-intro">
					<div>
						<h2 id="candidate-review-heading">${__("Revisión de candidatos")}</h2>
						<p>${__("Filtra, compara y clasifica aplicaciones por vacante. Las acciones masivas quedan auditadas.")}</p>
					</div>
					<div class="review-selection" aria-live="polite"><strong data-selected-count>0</strong> ${__("seleccionados")} · ${__("máximo 100 por lote")}</div>
				</div>
				<div class="review-status" data-review-status aria-live="polite"></div>
				<div class="alert alert-info hidden review-run-status" data-filtered-run-status aria-live="polite">
					<span data-filtered-run-summary></span>
					<div class="review-run-actions">
						<button class="btn btn-primary btn-sm" type="button" data-resume-filtered-run>${__("Reanudar cohorte")}</button>
						<button class="btn btn-default btn-sm hidden" type="button" data-review-skipped>${__("Revisar omitidos")}</button>
						<button class="btn btn-default btn-sm" type="button" data-cancel-filtered-run>${__("Cancelar cohorte")}</button>
					</div>
				</div>
				<div class="review-table-wrap" data-review-table-wrap aria-busy="false">
					<table class="table review-table">
						<thead>
							<tr>
								<th class="review-check"><input type="checkbox" data-select-all aria-label="Seleccionar todos los candidatos visibles"></th>
								<th>${__("Candidato")}</th>
								<th>${__("Vacante")}</th>
								<th>${__("Estado")}</th>
								<th>${__("Rating")}</th>
								<th>${__("Score AyP")}</th>
								<th>${__("Documento CV")}</th>
								<th>${__("Dedupe")}</th>
								<th>${__("Recibido")}</th>
								<th>${__("Acciones")}</th>
							</tr>
						</thead>
						<tbody data-review-rows></tbody>
					</table>
					<div class="review-empty hidden" data-review-empty>${__("No encontramos candidatos para estos filtros.")}</div>
				</div>
				<div class="review-footer">
					<button class="btn btn-default btn-sm hidden" type="button" data-load-more>${__("Cargar 50 más")}</button>
				</div>
			</section>`,
		).appendTo(this.page.main);
		this.$rows = this.$root.find("[data-review-rows]");
		this.$status = this.$root.find("[data-review-status]");
		this.$run_status = this.$root.find("[data-filtered-run-status]");
		this.$run_summary = this.$root.find("[data-filtered-run-summary]");
		this.$empty = this.$root.find("[data-review-empty]");
		this.$load_more = this.$root.find("[data-load-more]");
		this.$selected_count = this.$root.find("[data-selected-count]");
		this.$select_all = this.$root.find("[data-select-all]");
		this.$table_wrap = this.$root.find("[data-review-table-wrap]");
	}

	make_filters() {
		const reload = () => {
			clearTimeout(this.filter_timer);
			this.filter_timer = setTimeout(() => this.load_candidates({ reset: true }), 250);
		};
		this.filters = {
			job_title: this.page.add_field({
				fieldname: "job_title",
				label: __("Vacante"),
				fieldtype: "Link",
				options: "Job Opening",
				change: reload,
			}),
			status: this.page.add_field({
				fieldname: "status",
				label: __("Estado"),
				fieldtype: "Select",
				options: "\nOpen\nReplied\nShortlisted\nRejected\nHold\nAccepted",
				change: reload,
			}),
			source: this.page.add_field({
				fieldname: "source",
				label: __("Fuente"),
				fieldtype: "Link",
				options: "Job Applicant Source",
				change: reload,
			}),
			dedupe_status: this.page.add_field({
				fieldname: "dedupe_status",
				label: __("Dedupe"),
				fieldtype: "Select",
				options: "\nNuevo\nCoincidencia\nRevisión requerida\nManual",
				change: reload,
			}),
			cv_processing_status: this.page.add_field({
				fieldname: "cv_processing_status",
				label: __("Documento CV"),
				fieldtype: "Select",
				options:
					"\nSin CV\nPendiente\nProcesando\nProcesado\nRevisión manual\nIlegible\nProtegido\nNo compatible\nError de seguridad\nVerificado manualmente",
				change: reload,
			}),
			interview_queue: this.page.add_field({
				fieldname: "interview_queue",
				label: __("Cola de entrevistas"),
				fieldtype: "Select",
				options: [
					{ label: "", value: "" },
					{ label: __("Sin entrevista"), value: "no_interview" },
					{ label: __("Sin entrevistador asignado"), value: "unassigned" },
					{ label: __("Feedback pendiente"), value: "missing_feedback" },
					{ label: __("Con desacuerdo"), value: "disagreement" },
					{ label: __("Entrevista vencida"), value: "overdue" },
					{ label: __("Lista para decisión final"), value: "ready_final_decision" },
				],
				change: reload,
			}),
			minimum_rating: this.page.add_field({
				fieldname: "minimum_rating",
				label: __("Rating mínimo"),
				fieldtype: "Select",
				options: "\n1\n2\n3\n4\n5",
				change: reload,
			}),
			minimum_score: this.page.add_field({
				fieldname: "minimum_score",
				label: __("Score mínimo"),
				fieldtype: "Select",
				options: "\n60\n70\n80\n90",
				change: reload,
			}),
			sort_by: this.page.add_field({
				fieldname: "sort_by",
				label: __("Orden"),
				fieldtype: "Select",
				options: [
					{ label: __("Más recientes"), value: "received" },
					{ label: __("Mayor score AyP"), value: "score" },
				],
				default: "received",
				change: reload,
			}),
			search: this.page.add_field({
				fieldname: "search",
				label: __("Buscar"),
				fieldtype: "Data",
				placeholder: __("Nombre, correo, teléfono o ID"),
				change: reload,
			}),
		};
	}

	make_actions() {
		this.page.set_primary_action(__("Clasificar seleccionados"), () => this.show_batch_dialog(), "check");
		this.page.add_inner_button(__("Procesar todos los resultados filtrados"), () =>
			this.show_filtered_batch_dialog(),
		);
		this.page.add_inner_button(__("Comparar seleccionados"), () => this.show_compare_dialog());
		this.page.add_inner_button(__("Limpiar filtros"), () => {
			Object.values(this.filters).forEach((control) => control.set_value(""));
			this.load_candidates({ reset: true });
		});
		this.update_selection_ui();
	}

	bind_events() {
		this.$root.on("change", "[data-select-all]", (event) => {
			const checked = event.currentTarget.checked;
			this.rows.forEach((row) => {
				if (checked && this.selected.size < this.max_batch_size) this.selected.add(row.name);
				else if (!checked) this.selected.delete(row.name);
			});
			if (checked && this.rows.length > this.max_batch_size) {
				frappe.show_alert({
					message: __("Se seleccionaron los primeros 100 candidatos cargados."),
					indicator: "blue",
				});
			}
			this.render_rows();
		});
		this.$root.on("change", "[data-select-applicant]", (event) => {
			const name = event.currentTarget.dataset.selectApplicant;
			if (event.currentTarget.checked && this.selected.size >= this.max_batch_size) {
				event.currentTarget.checked = false;
				frappe.msgprint(__("Cada lote admite un máximo de 100 candidatos."));
				return;
			}
			if (event.currentTarget.checked) this.selected.add(name);
			else this.selected.delete(name);
			this.update_selection_ui();
		});
		this.$root.on("click", "[data-open-applicant]", (event) => {
			frappe.set_route("Form", "Job Applicant", event.currentTarget.dataset.openApplicant);
		});
		this.$root.on("click", "[data-open-profile]", (event) => {
			frappe.set_route("Form", "AYP Candidate Profile", event.currentTarget.dataset.openProfile);
		});
		this.$root.on("click", "[data-manage-profile]", (event) => {
			this.show_profile_dialog(event.currentTarget.dataset.manageProfile, false);
		});
		this.$root.on("click", "[data-resolve-dedupe]", (event) => {
			this.show_profile_dialog(event.currentTarget.dataset.resolveDedupe, true);
		});
		this.$root.on("click", "[data-score-applicant]", (event) => {
			this.show_scorecard_dialog(event.currentTarget.dataset.scoreApplicant);
		});
		this.$root.on("click", "[data-verify-document]", (event) => {
			this.show_document_verification_dialog(event.currentTarget.dataset.verifyDocument);
		});
		this.$root.on("click", "[data-schedule-interview]", async (event) => {
			await this.create_interview(event.currentTarget.dataset.scheduleInterview);
		});
		this.$root.on("click", "[data-open-interview]", (event) => {
			frappe.set_route("Form", "Interview", event.currentTarget.dataset.openInterview);
		});
		this.$root.on("click", "[data-final-decision]", (event) => {
			const button = event.currentTarget;
			this.confirm_final_decision(
				button.dataset.finalDecision,
				button.dataset.interview,
				button.dataset.targetStatus,
			);
		});
		this.$root.on("click", "[data-resume-filtered-run]", () => {
			if (this.active_filtered_run) this.confirm_frozen_run(this.active_filtered_run);
		});
		this.$root.on("click", "[data-cancel-filtered-run]", () => this.show_cancel_filtered_run_dialog());
		this.$root.on("click", "[data-review-skipped]", () => this.show_skipped_members_dialog());
		this.$root.on("click", "[data-retry-list]", () => this.load_candidates({ reset: true }));
		this.$load_more.on("click", () => this.load_candidates({ reset: false }));
	}

	async confirm_final_decision(applicant, interview, target_status) {
		frappe.confirm(
			__("¿Registrar la decisión final {0} desde la entrevista enviada {1}?", [target_status, interview]),
			async () => {
				await frappe.call({
					method: "hrms.hr.doctype.interview.interview.update_job_applicant_status",
					args: { status: target_status, job_applicant: applicant, interview },
					freeze: true,
					freeze_message: __("Registrando decisión final…"),
				});
				await this.load_candidates({ reset: true });
			},
		);
	}

	async create_interview(applicant) {
		const response = await frappe.call({
			method: "hrms.hr.doctype.job_applicant.job_applicant.create_interview",
			args: {
				job_applicant: applicant,
				interview_type: "AyP - Entrevista estructurada",
			},
			freeze: true,
			freeze_message: __("Preparando entrevista…"),
		});
		const doclist = frappe.model.sync(response.message);
		frappe.set_route("Form", doclist[0].doctype, doclist[0].name);
	}

	show_profile_dialog(applicant, resolveDedupe) {
		const dialog = new frappe.ui.Dialog({
			title: resolveDedupe ? __("Resolver deduplicación") : __("Decisión de Talent Pool"),
			fields: [
				...(resolveDedupe
					? [
							{
								fieldname: "identity_operation",
								fieldtype: "Select",
								label: __("Corrección de identidad"),
								options: [
									{ label: __("Separar esta solicitud en un perfil nuevo"), value: "split" },
									{ label: __("Revincular esta solicitud a otro perfil"), value: "relink" },
									{ label: __("Fusionar todo el perfil en otro perfil"), value: "merge" },
								],
								reqd: 1,
							},
							{
								fieldname: "target_profile",
								fieldtype: "Link",
								options: "AYP Candidate Profile",
								label: __("Perfil objetivo"),
								depends_on: "eval:doc.identity_operation=='relink' || doc.identity_operation=='merge'",
								mandatory_depends_on: "eval:doc.identity_operation=='relink' || doc.identity_operation=='merge'",
							},
						]
					: [
							{
								fieldname: "action",
								fieldtype: "Select",
								label: __("Decisión"),
								options: [
									{ label: __("Conservar activo"), value: "retain" },
									{ label: __("Marcar prioritario"), value: "priority" },
									{ label: __("Sin interés / no contactar"), value: "no_interest" },
									{ label: __("Disponer / no contactar"), value: "disposed" },
								],
								reqd: 1,
							},
						]),
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Motivo humano documentado"),
					description: resolveDedupe
						? __("Mínimo 20 caracteres. La fusión mueve todas las solicitudes y conserva ambos perfiles para auditoría.")
						: __("Mínimo 20 caracteres. La solicitud de eliminación usa el flujo de privacidad, no esta acción."),
					reqd: 1,
				},
			],
			primary_action_label: __("Guardar decisión"),
			primary_action: async (values) => {
				const execute = async (preview_binding = null) => {
					await frappe.call({
						method: resolveDedupe
							? "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.resolve_candidate_identity"
							: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.update_candidate_profile",
						args: resolveDedupe
							? {
									applicant,
									operation: values.identity_operation,
									target_profile: values.target_profile,
									reason: values.reason,
									preview_binding,
								}
							: { applicant, action: values.action, reason: values.reason },
						freeze: true,
						freeze_message: __("Guardando decisión de perfil…"),
					});
					dialog.hide();
					await this.load_candidates({ reset: true });
				};
				if (resolveDedupe && values.identity_operation === "merge") {
					const previewResponse = await frappe.call({
						method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.preview_candidate_identity",
						args: {
							applicant,
							operation: values.identity_operation,
							target_profile: values.target_profile,
						},
						freeze: true,
						freeze_message: __("Calculando impacto exacto…"),
					});
					const preview = previewResponse.message || {};
					const escape = frappe.utils.escape_html;
					const affected = (preview.moved_applications || []).map((name) => escape(name)).join(", ");
					frappe.confirm(
						`<strong>${__("Preview autoritativo de fusión")}</strong><br>
						${__("Origen")}: ${escape(preview.source_name || preview.source_profile || "")} (${escape(preview.source_profile || "")})<br>
						${__("Objetivo")}: ${escape(preview.target_name || preview.target_profile || "")} (${escape(preview.target_profile || "")})<br>
						${__("Solicitudes que se moverán")}: ${Number((preview.moved_applications || []).length)} · ${affected}<br>
						${__("Estado Talent Pool resultante")}: ${escape(preview.result_talent_pool_status || "")}<br>
						${__("No contactar resultante")}: ${preview.result_do_not_contact ? __("Sí") : __("No")}<br>
						${__("El perfil origen quedará como alias histórico no operativo.")}`,
						() => execute(preview.binding),
					);
					return;
				}
				await execute();
			},
		});
		dialog.show();
	}

	get_filter_values() {
		return Object.fromEntries(
			Object.entries(this.filters).map(([key, control]) => [key, control.get_value() || ""]),
		);
	}

	async load_candidates({ reset = false } = {}) {
		const generation = ++this.load_generation;
		const filter_snapshot = this.get_filter_values();
		if (this.loading && !reset) {
			this.load_generation -= 1;
			return;
		}
		this.loading = true;
		this.$table_wrap.attr("aria-busy", "true");
		this.$status.text(__("Cargando candidatos…"));
		if (reset) {
			this.start = 0;
			this.rows = [];
			this.selected.clear();
		}
		try {
			const response = await frappe.call({
				method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.get_candidates",
				args: {
					filters: filter_snapshot,
					start: this.start,
					page_length: this.page_length,
				},
			});
			if (generation !== this.load_generation) return;
			const payload = response.message || { rows: [], has_more: false, total_count: 0 };
			this.rows = reset ? payload.rows : [...this.rows, ...payload.rows];
			this.total_count = Number(payload.total_count || 0);
			this.has_more = Boolean(payload.has_more);
			this.start = this.rows.length;
			this.loaded = true;
			this.render_rows();
			this.$status.text(
				__("{0} de {1} candidatos cargados. {2} seleccionados.", [
					this.rows.length,
					this.total_count,
					this.selected.size,
				]),
			);
		} catch (error) {
			if (generation !== this.load_generation) return;
			this.$status.html(
				`${__("No se pudieron cargar los candidatos.")} <button class="btn btn-link btn-xs" type="button" data-retry-list>${__("Reintentar")}</button>`,
			);
			frappe.msgprint({
				title: __("Candidate Review"),
				message: __("No se pudieron cargar los candidatos. Intenta nuevamente."),
				indicator: "red",
			});
			throw error;
		} finally {
			if (generation === this.load_generation) {
				this.loading = false;
				this.$table_wrap.attr("aria-busy", "false");
			}
		}
	}

	render_rows() {
		const escape = frappe.utils.escape_html;
		this.$rows.empty();
		this.rows.forEach((row) => {
			const name = escape(row.name || "");
			const candidate = escape(row.applicant_name || row.name || "");
			const email = escape(row.email_id || "");
			const vacancy = escape(row.job_title || __("Sin vacante"));
			const status = escape(row.status || "");
			const dedupe = escape(row.custom_dedupe_status || __("Sin clasificar"));
			const rating = Number(row.rating_out_of_five || 0).toFixed(1);
			const score = row.custom_candidate_score;
			const recommendation = escape(row.custom_candidate_recommendation || __("Sin evaluar"));
			const document_status = escape(row.custom_cv_processing_status || __("Sin estado"));
			const document_method = escape(row.custom_cv_processing_method || "");
			const document_detail = escape(row.custom_cv_processing_detail || "");
			const document_ready = ["Procesado", "Verificado manualmente"].includes(
				row.custom_cv_processing_status,
			);
			const manual_reviewable = ["Sin CV", "Revisión manual", "Ilegible"].includes(
				row.custom_cv_processing_status,
			);
			const document_button = manual_reviewable
				? `<button class="btn btn-link btn-xs" type="button" data-verify-document="${name}">${__("Verificar CV manualmente")}</button>`
				: "";
			const score_button = document_ready
				? `<button class="btn btn-link btn-xs" type="button" data-score-applicant="${name}">${__("Evaluar")}</button>`
				: `<button class="btn btn-link btn-xs" type="button" disabled title="${__("El CV debe estar procesado o verificado manualmente")}">${__("Evaluar")}</button>`;
			const score_label = score === null || score === undefined || score === "" ? "—" : `${Number(score).toFixed(1)}/100`;
			const created = row.creation ? frappe.datetime.str_to_user(row.creation) : "—";
			const profile = row.custom_candidate_profile || "";
			const pool_status = escape(row.talent_pool_status || __("Sin decisión"));
			const contact_status = row.talent_pool_do_not_contact ? ` · ${__("No contactar")}` : "";
			const checked = this.selected.has(row.name) ? "checked" : "";
			const profile_button = profile
				? `<button class="btn btn-link btn-xs" type="button" data-open-profile="${escape(profile)}">${__("Perfil")}</button><button class="btn btn-link btn-xs" type="button" data-manage-profile="${name}">${__("Talent Pool")}</button><small>${pool_status}${contact_status}</small>${row.custom_dedupe_status === "Revisión requerida" || row.custom_dedupe_status === "Coincidencia" ? `<button class="btn btn-link btn-xs" type="button" data-resolve-dedupe="${name}">${__("Resolver dedupe")}</button>` : ""}`
				: "";
			const interview_button = row.existing_interview
				? `<button class="btn btn-link btn-xs" type="button" data-open-interview="${escape(row.existing_interview)}">${__("Abrir entrevista")}</button><small>${escape(row.interview_status || __("Borrador"))} · ${row.interview_scheduled_on ? escape(frappe.datetime.str_to_user(row.interview_scheduled_on)) : __("sin fecha")} · ${row.assigned_interviewers || 0} ${__("asignados")} · ${row.missing_feedback || 0} ${__("feedback pendientes")}${row.feedback_disagreement ? ` · ${__("hay desacuerdo")}` : ""}</small>`
				: row.status === "Shortlisted"
					? `<button class="btn btn-link btn-xs" type="button" data-schedule-interview="${name}">${__("Programar entrevista")}</button>`
					: "";
			const final_target =
				row.interview_status === "Cleared" ? "Accepted" : row.interview_status === "Rejected" ? "Rejected" : "";
			const final_decision_button =
				row.existing_interview &&
				Number(row.interview_docstatus) === 1 &&
				final_target &&
				!["Accepted", "Rejected"].includes(row.status)
					? `<button class="btn btn-primary btn-xs" type="button" data-final-decision="${name}" data-interview="${escape(row.existing_interview)}" data-target-status="${escape(final_target)}">${__("Decisión final")}</button>`
					: "";
			this.$rows.append(
				`<tr>
					<td class="review-check" data-label="${__("Seleccionar")}"><input type="checkbox" data-select-applicant="${name}" aria-label="${__("Seleccionar a {0}", [candidate])}" ${checked}></td>
					<td data-label="${__("Candidato")}"><button class="btn btn-link review-name" type="button" data-open-applicant="${name}">${candidate}</button><small>${email}</small></td>
					<td data-label="${__("Vacante")}">${vacancy}</td>
					<td data-label="${__("Estado")}"><span class="indicator-pill gray">${status}</span></td>
					<td data-label="${__("Rating")}"><strong>${rating}</strong>/5</td>
					<td data-label="${__("Score AyP")}"><strong>${escape(score_label)}</strong><small>${recommendation}</small></td>
					<td data-label="${__("Documento CV")}"><span class="indicator-pill ${document_ready ? "green" : "orange"}">${document_status}</span><small>${document_method}${document_method && document_detail ? " · " : ""}${document_detail}</small>${document_button}</td>
					<td data-label="${__("Dedupe")}">${dedupe}</td>
					<td data-label="${__("Recibido")}">${escape(created)}</td>
					<td class="review-mobile-actions" data-label="${__("Acciones")}">${final_decision_button}<button class="btn btn-link btn-xs" type="button" data-open-applicant="${name}">${__("Revisar")}</button>${score_button}${interview_button}${profile_button}</td>
				</tr>`,
			);
		});
		this.$empty.toggleClass("hidden", this.rows.length > 0);
		this.$load_more.toggleClass("hidden", !this.has_more);
		this.update_selection_ui();
	}

	update_selection_ui() {
		this.$selected_count.text(this.selected.size);
		this.$select_all.prop(
			"checked",
			this.rows.length > 0 && this.rows.every((row) => this.selected.has(row.name)),
		);
		this.page.btn_primary && this.page.btn_primary.prop("disabled", this.selected.size === 0);
	}

	show_batch_dialog() {
		if (!this.selected.size) {
			frappe.msgprint(__("Selecciona al menos un candidato."));
			return;
		}
		if (this.selected.size > this.max_batch_size) {
			frappe.msgprint(__("Reduce la selección a un máximo de 100 candidatos."));
			return;
		}
		const job_title = this.filters.job_title.get_value();
		if (!job_title) {
			frappe.msgprint(__("Selecciona una vacante antes de ejecutar una acción masiva."));
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Clasificar {0} candidatos", [this.selected.size]),
			fields: [
				{
					fieldname: "target_status",
					fieldtype: "Select",
					label: __("Estado destino"),
					options: "Replied\nShortlisted\nHold\nRejected\nOpen",
					reqd: 1,
				},
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Motivo documentado"),
					reqd: 1,
				},
			],
			primary_action_label: __("Continuar"),
			primary_action: (values) => {
				dialog.hide();
				frappe.confirm(
					__("¿Cambiar {0} candidatos de la vacante {1} a {2}?", [
						this.selected.size,
						job_title,
						values.target_status,
					]),
					() => this.apply_batch_action(values, job_title),
				);
			},
		});
		dialog.show();
	}

	async apply_batch_action(values, job_title) {
		const response = await frappe.call({
			method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.apply_batch_action",
			args: {
				applicant_names: [...this.selected],
				target_status: values.target_status,
				reason: values.reason,
				job_title,
			},
			freeze: true,
			freeze_message: __("Clasificando candidatos…"),
		});
		const result = response.message || {};
		frappe.show_alert({
			message: __("{0} candidatos actualizados · lote {1}", [result.updated || 0, result.batch_id || "—"]),
			indicator: "green",
		});
		this.selected.clear();
		await this.load_candidates({ reset: true });
	}

	show_filtered_batch_dialog() {
		const filters = this.get_filter_values();
		if (!filters.job_title || !filters.status) {
			frappe.msgprint(__("Fija una vacante y un estado de origen antes de procesar todos los resultados."));
			return;
		}
		const dialog = new frappe.ui.Dialog({
			title: __("Congelar resultados filtrados"),
			fields: [
				{
					fieldname: "target_status",
					fieldtype: "Select",
					label: __("Estado destino"),
					options: "Replied\nShortlisted\nHold\nRejected\nOpen",
					reqd: 1,
				},
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Motivo documentado para todos los lotes"),
					reqd: 1,
				},
			],
			primary_action_label: __("Congelar alcance exacto"),
			primary_action: async (values) => {
				dialog.hide();
				await this.freeze_filtered_run(values, filters);
			},
		});
		dialog.show();
	}

	async freeze_filtered_run(values, filters) {
		const response = await frappe.call({
			method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.freeze_filtered_run",
			args: { filters, target_status: values.target_status, reason: values.reason },
			freeze: true,
			freeze_message: __("Congelando la cohorte exacta…"),
		});
		const run = response.message || {};
		this.active_filtered_run = run;
		this.render_filtered_run(run);
		this.confirm_frozen_run(run);
	}

	confirm_frozen_run(run) {
		frappe.confirm(
			__("La cohorte {0} contiene exactamente {1} candidatos. Vacante: {2}. Estado origen: {3}. Estado destino: {4}. Motivo: {5}. ¿Procesarla en lotes auditados de máximo 100?", [
				run.run,
				run.total,
				run.job_title,
				run.source_status,
				run.target_status,
				run.reason,
			]),
			() => this.process_filtered_run(run.run),
		);
	}

	async process_filtered_run(run_name) {
		let state = this.active_filtered_run || { run: run_name, processed: 0, skipped: 0, remaining: 0 };
		try {
			do {
				const response = await frappe.call({
					method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.process_filtered_run_chunk",
					args: { run: run_name },
					freeze: true,
					freeze_message: __("Procesando cohorte · {0} completados…", [state.processed || 0]),
				});
				state = response.message || state;
				this.active_filtered_run = state;
				this.render_filtered_run(state);
			} while (Number(state.remaining || 0) > 0);
			if (Number(state.skipped || 0) > 0) {
				frappe.msgprint({
					title: __("Cohorte terminada con omitidos por revisar"),
					message: __("{0} procesados y {1} omitidos. Revisa cada omitido antes de cerrar la cohorte.", [
					state.processed || 0,
					state.skipped || 0,
					]),
					indicator: "orange",
				});
			} else {
				frappe.show_alert({
					message: __("Cohorte completada: {0} procesados, sin omitidos.", [state.processed || 0]),
					indicator: "green",
				});
				this.active_filtered_run = null;
			}
			this.render_filtered_run(this.active_filtered_run);
		} catch (error) {
			frappe.msgprint(
				__("Cohorte pausada: {0} procesados, {1} omitidos y {2} pendientes. Usa Reanudar cohorte.", [
					state.processed || 0,
					state.skipped || 0,
					state.remaining || 0,
				]),
			);
			throw error;
		} finally {
			await this.load_candidates({ reset: true });
		}
	}

	async load_active_filtered_run() {
		const response = await frappe.call({
			method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.get_active_filtered_run",
		});
		this.active_filtered_run = response.message || null;
		this.render_filtered_run(this.active_filtered_run);
	}

	render_filtered_run(run) {
		this.$run_status.toggleClass("hidden", !run);
		if (!run) {
			this.$run_summary.text("");
			return;
		}
		this.$run_summary.text(
			__("Cohorte {0} · Vacante: {1} · Estado origen: {2} → Estado destino: {3} · Motivo: {4} · {5} procesados · {6} omitidos · {7} pendientes.", [
				run.run,
				run.job_title || "—",
				run.source_status || "—",
				run.target_status || "—",
				run.reason || "—",
				run.processed || 0,
				run.skipped || 0,
				run.remaining || 0,
			]),
		);
		const completedWithSkips = run.run_status === "Completed" && Number(run.skipped || 0) > 0;
		this.$run_status.find("[data-resume-filtered-run]").toggleClass("hidden", run.run_status === "Completed");
		this.$run_status
			.find("[data-cancel-filtered-run]")
			.toggleClass("hidden", !["Frozen", "In Progress"].includes(run.run_status));
		this.$run_status.find("[data-review-skipped]").toggleClass("hidden", !completedWithSkips);
	}

	show_cancel_filtered_run_dialog() {
		const run = this.active_filtered_run;
		if (!run) return;
		const dialog = new frappe.ui.Dialog({
			title: __("Cancelar cohorte congelada"),
			fields: [{ fieldname: "reason", fieldtype: "Small Text", label: __("Motivo de cancelación"), reqd: 1 }],
			primary_action_label: __("Cancelar sin borrar historial"),
			primary_action: async (values) => {
				await frappe.call({
					method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.cancel_filtered_run",
					args: { run: run.run, reason: values.reason },
					freeze: true,
				});
				dialog.hide();
				this.active_filtered_run = null;
				this.render_filtered_run(null);
			},
		});
		dialog.show();
	}

	async show_skipped_members_dialog() {
		const run = this.active_filtered_run;
		if (!run) return;
		const response = await frappe.call({
			method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.get_filtered_run_members",
			args: { run: run.run, member_status: "Skipped" },
		});
		const escape = frappe.utils.escape_html;
		const rows = (response.message || [])
			.map((member) => `<tr><td><a class="btn btn-link btn-xs" href="/app/job-applicant/${encodeURIComponent(member.applicant)}">${escape(member.applicant)}</a></td><td>${escape(member.frozen_status || "")}</td><td>${escape(member.previous_status || "")}</td><td>${escape(member.outcome_detail || "")}</td></tr>`)
			.join("");
		const dialog = new frappe.ui.Dialog({
			title: __("Omitidos de la cohorte {0}", [run.run]),
			size: "large",
			fields: [
				{ fieldname: "members", fieldtype: "HTML", options: `<div class="review-table-wrap"><table class="table"><thead><tr><th>${__("Candidato")}</th><th>${__("Estado congelado")}</th><th>${__("Estado actual")}</th><th>${__("Motivo")}</th></tr></thead><tbody>${rows}</tbody></table></div>` },
				{ fieldname: "reason", fieldtype: "Small Text", label: __("Cierre documentado de los omitidos"), reqd: 1 },
			],
			primary_action_label: __("Confirmar revisión y cerrar"),
			primary_action: async (values) => {
				await frappe.call({
					method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.acknowledge_filtered_run_skips",
					args: { run: run.run, reason: values.reason },
					freeze: true,
				});
				dialog.hide();
				this.active_filtered_run = null;
				this.render_filtered_run(null);
			},
		});
		dialog.show();
	}

	show_document_verification_dialog(applicant) {
		const dialog = new frappe.ui.Dialog({
			title: __("Verificación manual del CV · {0}", [applicant]),
			fields: [
				{
					fieldname: "reason",
					fieldtype: "Small Text",
					label: __("Evidencia de revisión manual"),
					description: __(
						"Indica qué documento o información verificaste. No uses esta vía para archivos protegidos o con error de seguridad.",
					),
					reqd: 1,
				},
			],
			primary_action_label: __("Registrar verificación"),
			primary_action: async (values) => {
				await frappe.call({
					method:
						"hrms.hr.page.ayp_candidate_review.ayp_candidate_review.verify_candidate_document_manually",
					args: { applicant, reason: values.reason },
					freeze: true,
					freeze_message: __("Registrando verificación documental…"),
				});
				dialog.hide();
				await this.load_candidates({ reset: true });
			},
		});
		dialog.show();
	}

	async show_scorecard_dialog(applicant) {
		const response = await frappe.call({
			method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.get_scorecard",
			args: { applicant },
			freeze: true,
			freeze_message: __("Cargando scorecard…"),
		});
		const payload = response.message || { criteria: [], latest: null };
		const fields = [];
		if (payload.latest) {
			fields.push({
				fieldname: "latest_summary",
				fieldtype: "HTML",
				options: `<div class="scorecard-latest"><strong>${__("Última evaluación")}: ${frappe.utils.escape_html(
					String(payload.latest.total_score),
				)}/100</strong><span>${frappe.utils.escape_html(payload.latest.recommendation || "")}</span></div>`,
			});
		}
		payload.criteria.forEach((criterion) => {
			fields.push({
				fieldname: `section__${criterion.criterion_key}`,
				fieldtype: "Section Break",
				label: `${criterion.criterion_label} · ${criterion.weight}%`,
				description: criterion.description,
			});
			fields.push({
				fieldname: `rating__${criterion.criterion_key}`,
				fieldtype: "Select",
				label: __("Calificación (0–5)"),
				options: "0\n1\n2\n3\n4\n5",
				default: String(criterion.rating || 0),
				reqd: 1,
			});
			fields.push({
				fieldname: `evidence__${criterion.criterion_key}`,
				fieldtype: "Small Text",
				label: __("Evidencia comprobable"),
				description: __("Describe una observación verificable de al menos 20 caracteres; máximo 500."),
				default: criterion.evidence || "",
				reqd: 1,
			});
		});
		const dialog = new frappe.ui.Dialog({
			title: __("Scorecard AyP · {0}", [applicant]),
			fields,
			primary_action_label: __("Guardar nueva evaluación"),
			primary_action: async (values) => {
				const criteria = payload.criteria.map((criterion) => ({
					criterion_key: criterion.criterion_key,
					rating: values[`rating__${criterion.criterion_key}`],
					evidence: values[`evidence__${criterion.criterion_key}`],
				}));
				const save_response = await frappe.call({
					method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.save_scorecard",
					args: { applicant, criteria },
					freeze: true,
					freeze_message: __("Calculando y guardando scorecard…"),
				});
				const result = save_response.message || {};
				dialog.hide();
				frappe.msgprint({
					title: __("Evaluación guardada"),
					message: `<strong>${frappe.utils.escape_html(String(result.total_score))}/100</strong><br>${frappe.utils.escape_html(
						result.recommendation || "",
					)}<p class="text-muted">${__("La recomendación es asistida; la decisión final corresponde a una persona.")}</p>`,
					indicator: "green",
				});
				await this.load_candidates({ reset: true });
			},
		});
		dialog.show();
	}

	async show_compare_dialog() {
		const selected_rows = this.rows.filter((row) => this.selected.has(row.name));
		if (selected_rows.length < 2 || selected_rows.length > 4) {
			frappe.msgprint(__("Selecciona entre 2 y 4 candidatos visibles para comparar."));
			return;
		}
		const vacancies = new Set(selected_rows.map((row) => row.job_title).filter(Boolean));
		if (vacancies.size !== 1 || selected_rows.some((row) => !row.job_title)) {
			frappe.msgprint(__("La comparación exige candidatos de una misma vacante."));
			return;
		}
		const escape = frappe.utils.escape_html;
		const scorecard_responses = await Promise.all(
			selected_rows.map((row) =>
				frappe.call({
					method: "hrms.hr.page.ayp_candidate_review.ayp_candidate_review.get_scorecard",
					args: { applicant: row.name },
				}),
			),
		);
		const scorecards = Object.fromEntries(
			selected_rows.map((row, index) => [row.name, scorecard_responses[index].message || {}]),
		);
		const ranked = [...selected_rows].sort((left, right) =>
			Number(right.custom_candidate_score || -1) - Number(left.custom_candidate_score || -1),
		);
		const rows = ranked
			.map((row) => {
				const scorecard = scorecards[row.name] || {};
				const latest = scorecard.latest || {};
				const criterion_evidence = (scorecard.criteria || [])
					.map(
						(criterion) => `<li><strong>${escape(criterion.criterion_label || criterion.criterion_key || "")}: ${Number(
							criterion.rating || 0,
						).toFixed(0)}/5</strong><br>${escape(criterion.evidence || __("Sin evidencia registrada"))}</li>`,
					)
					.join("");
				const audit = latest.name
					? `${escape(latest.scored_by || "")} · ${latest.scored_on ? escape(frappe.datetime.str_to_user(latest.scored_on)) : ""}`
					: __("Sin evaluación versionada");
				return `<tr>
					<td><strong>${escape(row.applicant_name || row.name)}</strong><small>${escape(row.name)}</small></td>
					<td>${escape(row.status || "")}</td>
					<td>${row.custom_candidate_score === null || row.custom_candidate_score === undefined ? "—" : `${Number(row.custom_candidate_score).toFixed(1)}/100`}<small>${escape(row.custom_candidate_recommendation || __("Sin evaluar"))}</small></td>
					<td><ol class="review-evidence-list">${criterion_evidence || `<li>${__("Sin criterios evaluados")}</li>`}</ol><small>${audit}</small></td>
					<td>${Number(row.rating_out_of_five || 0).toFixed(1)}/5</td>
					<td>${escape(row.custom_dedupe_status || __("Sin clasificar"))}</td>
				</tr>`;
			})
			.join("");
		const dialog = new frappe.ui.Dialog({
			title: __("Comparador · {0}", [[...vacancies][0]]),
			size: "extra-large",
			fields: [
				{
					fieldname: "comparison",
					fieldtype: "HTML",
					options: `<div class="review-comparison"><p>${__("Orden visual por score; decide usando la evidencia y el contexto de la vacante.")}</p><div class="review-table-wrap"><table class="table"><thead><tr><th>${__("Candidato")}</th><th>${__("Estado")}</th><th>${__("Score")}</th><th>${__("Criterios y evidencia")}</th><th>${__("Rating")}</th><th>${__("Dedupe")}</th></tr></thead><tbody>${rows}</tbody></table></div></div>`,
				},
			],
		});
		dialog.show();
	}
}
