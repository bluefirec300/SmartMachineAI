from __future__ import annotations

import sys
from pathlib import Path

import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from config.environment import get_active_environment
from ui import auth
from ui.factory_config_data import get_plants
from ui.production_data import (
    create_product,
    get_batch_status_counts,
    get_batches,
    get_product_categories,
    get_products,
    is_production_simulator_running,
    update_product,
)


current_user = auth.current_user()
can_edit = auth.can_edit("admin")

st.title("🏭 Production Context")
st.caption(
    "Product master data and simulated production batch history - context "
    "for later energy-per-production analytics (Phase 6+). Not yet a "
    "production analytics dashboard - this page is a browsable record, "
    "not OEE/efficiency reporting."
)

if not can_edit:
    st.info("View-only - only Administrator accounts can edit the product master.")

tab_products, tab_batches, tab_simulator = st.tabs(["Products", "Production Batches", "Simulator"])


# ---------------------------------------------------------------------------
# Products
# ---------------------------------------------------------------------------

with tab_products:
    categories = get_product_categories()
    products = get_products()

    st.dataframe(
        [
            {
                "Code": p["product_code"], "Name": p["product_name"], "Category": p["category_name"] or "-",
                "Variant": p["variant"] or "-", "Std. batch size": p["standard_batch_size"],
                "Unit": p["unit_of_measure"], "Active": "Yes" if p["active"] else "No",
                "Source": p["source"],
            }
            for p in products
        ],
        hide_index=True, width="stretch",
    )

    if can_edit:
        with st.expander("Add a new product"):
            with st.form("new_product_form"):
                product_code = st.text_input("Product code")
                product_name = st.text_input("Product name")
                category_choice = st.selectbox("Category", categories, format_func=lambda c: c["name"])
                variant = st.text_input("Variant / colour (optional)")
                standard_batch_size = st.number_input("Standard batch size", min_value=0.0, value=100.0)
                unit_of_measure = st.selectbox("Unit of measure", ["kg", "litre", "tonne", "other"])
                recipe_reference = st.text_input("Recipe reference (optional label, not ingredients)")

                if st.form_submit_button("Add product"):
                    if product_code.strip() and product_name.strip():
                        create_product(
                            product_code.strip(), product_name.strip(), category_choice["id"],
                            variant or None, standard_batch_size or None, unit_of_measure,
                            recipe_reference or None, current_user["username"],
                        )
                        st.success(f"Product '{product_code}' added.")
                        st.rerun()
                    else:
                        st.error("Enter a product code and name.")

        with st.expander("Edit an existing product"):
            product_options = {f"{p['product_code']} - {p['product_name']}": p for p in products}
            if product_options:
                selected_label = st.selectbox("Product", sorted(product_options), key="edit_product_select")
                product = product_options[selected_label]

                with st.form(f"edit_product_form_{product['id']}"):
                    edit_name = st.text_input("Product name", value=product["product_name"])
                    edit_variant = st.text_input("Variant / colour", value=product["variant"] or "")
                    edit_batch_size = st.number_input("Standard batch size", min_value=0.0, value=float(product["standard_batch_size"] or 0.0))
                    edit_active = st.checkbox("Active", value=bool(product["active"]))

                    if st.form_submit_button("Save changes"):
                        update_product(
                            product["id"], current_user["username"],
                            product_name=edit_name, variant=edit_variant or None,
                            standard_batch_size=edit_batch_size or None, active=1 if edit_active else 0,
                        )
                        st.success(f"'{selected_label}' saved.")
                        st.rerun()


# ---------------------------------------------------------------------------
# Production Batches - read-only browsable log, not analytics
# ---------------------------------------------------------------------------

with tab_batches:
    plants = get_plants()
    filter_cols = st.columns(3)
    with filter_cols[0]:
        plant_filter = st.selectbox(
            "Plant", [None, *plants], format_func=lambda p: "All plants" if p is None else p["code"].upper(),
            key="batch_plant_filter",
        )
    with filter_cols[1]:
        status_filter = st.selectbox(
            "Status", [None, "planned", "running", "completed", "cancelled", "interrupted"],
            format_func=lambda s: "All statuses" if s is None else s.title(),
            key="batch_status_filter",
        )
    with filter_cols[2]:
        st.write("")

    running_batches = get_batches(
        plant_id=plant_filter["id"] if plant_filter else None,
        status="running", limit=50,
    )
    if running_batches:
        st.subheader(f"Currently running ({len(running_batches)})")
        st.dataframe(
            [
                {
                    "Batch": b["batch_code"], "Plant": b["plant_code"].upper(), "Equipment": b["equipment_display_name"],
                    "Product": b["product_name"], "Planned": b["planned_quantity"], "Actual so far": round(b["actual_quantity"] or 0, 1),
                    "Unit": b["unit_of_measure"], "Started": b["start_time"],
                }
                for b in running_batches
            ],
            hide_index=True, width="stretch",
        )
        st.divider()

    st.subheader("Batch history")
    if status_filter == "running":
        history = []  # already shown in "Currently running" above
    else:
        history = get_batches(
            plant_id=plant_filter["id"] if plant_filter else None,
            status=status_filter,
            limit=200,
        )

    st.dataframe(
        [
            {
                "Batch": b["batch_code"], "Plant": b["plant_code"].upper(), "Equipment": b["equipment_display_name"],
                "Product": b["product_name"], "Status": b["status"].title(), "Planned": b["planned_quantity"],
                "Actual": b["actual_quantity"], "Good": round(b["good_quantity"] or 0, 1) if b["good_quantity"] is not None else None,
                "Reject": round(b["reject_quantity"] or 0, 1) if b["reject_quantity"] is not None else None,
                "Unit": b["unit_of_measure"], "Start": b["start_time"], "End": b["end_time"],
                "Downtime (min)": b["downtime_minutes"], "Downtime reason": b["downtime_reason"] or "-",
                "Source": b["source"],
            }
            for b in history
        ],
        hide_index=True, width="stretch", height=400,
    )


# ---------------------------------------------------------------------------
# Simulator status
# ---------------------------------------------------------------------------

with tab_simulator:
    if get_active_environment() != "simulation":
        st.info("Not applicable outside the simulation environment.")
    else:
        running = is_production_simulator_running()

        if running:
            st.success("production_simulator.service is running.")
        else:
            st.error(
                "production_simulator.service does not appear to be running. "
                "Production batch history will not advance until it's started."
            )

        st.caption(
            "This runs as its own always-on background process (production_simulator.service), "
            "independent of this page - it keeps generating batch history even when nobody has "
            "this page open."
        )

        counts = get_batch_status_counts()
        if counts:
            st.subheader("All-time batch status counts")
            cols = st.columns(len(counts))
            for col, (status, count) in zip(cols, sorted(counts.items())):
                col.metric(status.title(), count)
        else:
            st.caption("No batches generated yet.")

        st.caption(
            "Note: the pre-existing ProductCode/BatchNumber/GoodCount/RejectCount tags on "
            "production equipment (visible on Live Data) are legacy/cosmetic - they were never "
            "wired to a real batch lifecycle and are NOT yet synchronized with the production "
            "batches shown on this page. production_batches is the authoritative simulated "
            "production context as of Phase 4. Reconciling the two into one production reality "
            "is planned for Phase 5 (Simulation Realism)."
        )
