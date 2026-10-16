"""Statutory employer contributions, for both the salary slip and CTC.

Each statute exposes ``get_employer_contributions()`` returning
``{salary_component: amount}``. Adding one means a function and a registry
entry; neither hook below changes.
"""


"""Calculate employer statutory contributions for CTC."""

import frappe
from frappe.utils import flt

from india_payroll.india_payroll import esi, epf
from india_payroll.india_payroll.utils import get_slip_ssa_values

CONTRIBUTION_SOURCES = (esi.get_employer_contributions,)
CONFIG_FIELDS = ("is_person_with_disability",)

EMPLOYER_PF_COMPONENTS = {
    "epf": "Employer Provident Fund",
    "eps": "Employer Pension Scheme",
    "edli": "Employees Deposit Linked Insurance",
    "admin": "EPF Admin Charges",
}


def _value(row, fieldname, default=None):
    """Read a field from a dictionary or Frappe document row."""
    if isinstance(row, dict):
        return row.get(fieldname, default)
    return getattr(row, fieldname, default)


def get_employer_pf_contributions(earnings, assignment):
    """Calculate employer PF costs for one full salary cycle.

    The assignment's effective date determines the applicable wage ceiling.
    PF wages are Basic/DA earnings, not gross salary.
    """
    if not assignment.get("epf_applicable"):
        return {}

    pf_wage = sum(
        flt(_value(row, "default_amount"))
        for row in earnings
        if epf.is_pf_wage_component(_value(row, "salary_component"))
    )

    if pf_wage <= 0:
        return {}

    effective_date = assignment.get("from_date")
    if not effective_date:
        frappe.throw(
            "Set the Salary Structure Assignment effective date "
            "before calculating employer PF contributions."
        )

    ceilings = epf.get_epf_wage_ceilings(effective_date)
    capped_wage = epf.cap_pf_wage(pf_wage, ceilings)

    # Employer EPF may use actual PF wages when the assignment enables it.
    employer_wage = (
        pf_wage
        if assignment.get("contribute_on_actual_pf_wage")
        else capped_wage
    )

    # EPS eligibility follows the existing India Payroll implementation.
    eps_wage = epf.get_eps_wage(pf_wage, ceilings)
    eps_amount = epf._epfo_round(eps_wage * epf.EPS_RATE)

    employer_total = epf._epfo_round(
        employer_wage * epf.EPF_EMPLOYER_RATE
    )
    employer_epf = max(0, employer_total - eps_amount)

    edli_amount = epf._epfo_round(capped_wage * epf.EDLI_RATE)
    admin_amount = epf._epfo_round(capped_wage * epf.EPF_ADMIN_RATE)

    return {
        EMPLOYER_PF_COMPONENTS["epf"]: employer_epf,
        EMPLOYER_PF_COMPONENTS["eps"]: eps_amount,
        EMPLOYER_PF_COMPONENTS["edli"]: edli_amount,
        EMPLOYER_PF_COMPONENTS["admin"]: admin_amount,
    }


def compute_employer_contributions(
    earnings, config, *, paid_field="amount", company=None
) -> dict:
    amounts = {}
    for source in CONTRIBUTION_SOURCES:
        amounts.update(
            source(
                earnings,
                config,
                paid_field=paid_field,
                company=company,
            )
        )
    return amounts


def apply_regional_ctc_components(assignment, rows_by_type, data) -> None:
    """Populate employer statutory costs in Salary Structure Assignment CTC."""
    earnings = rows_by_type.get("earnings") or []

    contributions = compute_employer_contributions(
        earnings,
        assignment,
        paid_field="default_amount",
        company=assignment.company,
    )

    contributions.update(get_employer_pf_contributions(earnings, assignment))

    for component, amount in contributions.items():
        if (
            flt(amount) > 0
            and frappe.db.exists("Salary Component", component)
        ):
            assignment.upsert_employer_contribution(
                rows_by_type, data, component, amount
            )


def set_slip_employer_contributions(doc) -> None:
    """Show employer contributions on the slip without changing net pay."""
    if not doc.salary_structure:
        return

    config = get_slip_ssa_values(doc, list(CONFIG_FIELDS))
    amounts = compute_employer_contributions(
        doc.earnings,
        config,
        company=doc.company,
    )

    for component, amount in amounts.items():
        doc.employer_contributions = [
            row
            for row in doc.employer_contributions
            if row.salary_component != component
        ]

        if (
            flt(amount) > 0
            and frappe.db.exists("Salary Component", component)
        ):
            doc.append(
                "employer_contributions",
                {"salary_component": component, "amount": amount},
            )
