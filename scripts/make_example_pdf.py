#!/usr/bin/env python3
"""Generate the demo SOP document: docs/examples/appointment_scheduling_sop.pdf.
A realistic clinic phone-line procedure — used to test/demo PDF ingestion."""
from pathlib import Path

from reportlab.lib.enums import TA_JUSTIFY
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer

OUT = Path(__file__).resolve().parents[1] / "docs" / "examples" / "appointment_scheduling_sop.pdf"

styles = getSampleStyleSheet()
H1 = ParagraphStyle("H1", parent=styles["Title"], fontSize=17, spaceAfter=4)
META = ParagraphStyle("META", parent=styles["Normal"], fontSize=9.5, textColor="#555555", spaceAfter=14)
H2 = ParagraphStyle("H2", parent=styles["Heading2"], fontSize=12.5, spaceBefore=12, spaceAfter=4)
P = ParagraphStyle("P", parent=styles["Normal"], fontSize=10.5, leading=15, alignment=TA_JUSTIFY, spaceAfter=6)
LI = ParagraphStyle("LI", parent=P, leftIndent=16, bulletIndent=6)

doc = SimpleDocTemplate(str(OUT), pagesize=A4, leftMargin=2.2 * cm, rightMargin=2.2 * cm,
                        topMargin=2 * cm, bottomMargin=2 * cm,
                        title="Patient Appointment Scheduling & Triage — Call Handling SOP")

flow = [
    Paragraph("Patient Appointment Scheduling &amp; Triage", H1),
    Paragraph("Call Handling Standard Operating Procedure — Meridian Health Clinics · Document CS-SOP-014 · "
              "Revision 6 · Effective 01 Jun 2026 · Owner: Patient Services", META),

    Paragraph("1. Purpose and scope", H2),
    Paragraph("This procedure governs all inbound appointment calls handled by the Patient Services line. It "
              "applies to scheduling, rescheduling and cancellation of outpatient appointments. It does not "
              "apply to emergency lines. Agents must follow the stages in order unless a triage rule in "
              "section 4 applies.", P),

    Paragraph("2. Call opening and identification", H2),
    Paragraph("2.1 Greet the caller with the clinic name and offer assistance. The agent must state: "
              "“This call may be recorded for quality and training purposes.” before any personal "
              "details are collected.", P),
    Paragraph("2.2 Verify the patient's identity before discussing any medical or appointment information. "
              "Acceptable verification: date of birth plus either the patient ID or the registered postcode. "
              "If the caller is not the patient, confirm they are a registered proxy in the patient record "
              "system; otherwise politely decline to proceed.", P),

    Paragraph("3. Understanding the request", H2),
    Paragraph("3.1 Establish whether the caller wants a new appointment, a reschedule, or a cancellation.", P),
    Paragraph("3.2 For new appointments, capture the reason for the visit in the patient's own words and look "
              "up the appropriate clinic and practitioner in the scheduling system. Check the patient record "
              "for outstanding referrals before offering slots.", P),
    Paragraph("3.3 Offer the earliest two available slots from the scheduling system. If neither suits, offer "
              "the waiting-list option. Required wording when the wait exceeds ten working days: “You can "
              "also contact us any weekday morning, as cancellations are released at 8am.”", P),

    Paragraph("4. Triage rules (override normal flow)", H2),
    Paragraph("4.1 If at any point the caller describes chest pain, difficulty breathing, sudden weakness, or "
              "uncontrolled bleeding, the agent must stop scheduling immediately, advise calling the emergency "
              "number, and transfer the call to the duty nurse. This overrides every other stage.", P),
    Paragraph("4.2 If the caller reports symptoms worsening within the last 48 hours, book into the urgent "
              "same-week clinic rather than routine slots, subject to availability in the scheduling system.", P),

    Paragraph("5. Handling common situations", H2),
    Paragraph("5.1 <b>Fee questions.</b> Quote only the fees from the price schedule in the billing system; "
              "never estimate from memory. Missed-appointment fees may be waived once per year for callers "
              "with no prior no-shows — check the patient record first.", P),
    Paragraph("5.2 <b>Frustrated callers.</b> Acknowledge the inconvenience, do not interrupt, and offer the "
              "earliest concrete option. Do not promise callbacks from practitioners.", P),
    Paragraph("5.3 <b>Repeated rescheduling.</b> If a patient reschedules the same appointment a third time, "
              "flag the booking for the practice manager but proceed normally with the caller.", P),

    Paragraph("6. Confirmation and closing", H2),
    Paragraph("6.1 Before closing, confirm: date, time, clinic location, practitioner name, and preparation "
              "instructions from the appointment type notes. Send the confirmation SMS while on the call.", P),
    Paragraph("6.2 A call is successful when an appointment is booked, rescheduled or cancelled as requested "
              "and confirmed by the caller, or when a triage transfer is completed. If the caller declines all "
              "options and the waiting list, thank them and close politely.", P),
    Paragraph("6.3 Never end the call while the caller is still asking questions. Always ask: “Is there "
              "anything else I can help you with today?”", P),

    Spacer(1, 10),
    Paragraph("Systems referenced: patient record system (identity, proxies, referrals, no-show history), "
              "scheduling system (slots, urgent clinic availability), billing system (price schedule). "
              "All lookups are read-only during the call except the booking confirmation itself.", META),
]

OUT.parent.mkdir(parents=True, exist_ok=True)
doc.build(flow)
print(f"written: {OUT} ({OUT.stat().st_size} bytes)")
