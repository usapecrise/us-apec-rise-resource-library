from locale import normalize

from azure.storage.blob import BlobServiceClient
import zipfile

import dash
from dash import Dash, html, dcc, Input, Output, State, ctx, ALL
import dash_bootstrap_components as dbc
import plotly.express as px
import plotly.graph_objects as go
import uuid

import pandas as pd
import numpy as np
import requests
import feedparser
from dash.exceptions import PreventUpdate

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from collections import Counter

import io
import os
import re
import json
import sys

from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image, Table, TableStyle
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.lib.pagesizes import landscape, letter
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase import pdfmetrics
from reportlab.lib.units import inch

ET = ZoneInfo("America/New_York")

import logging
from concurrent.futures import ThreadPoolExecutor

from dotenv import load_dotenv
from pathlib import Path

env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=env_path)

# =====================================
# APP SETUP
# =====================================

external_stylesheets = [
    dbc.themes.BOOTSTRAP,
    "https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/bootstrap-icons.css",
    "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap"
]

app = dash.Dash(__name__, external_stylesheets=external_stylesheets)
server = app.server

# ==============================
# AIRTABLE CONFIGURATION
# ==============================

AIRTABLE_BASE_ID = os.getenv("AIRTABLE_BASE_ID")
AIRTABLE_API_KEY = os.getenv("AIRTABLE_API_KEY")

if not AIRTABLE_BASE_ID or not AIRTABLE_API_KEY:
    raise ValueError("Airtable environment variables not set")

HEADERS = {
    "Authorization": f"Bearer {AIRTABLE_API_KEY}"
}

PERCENT_INDICATORS = {"OC3", "OC4", "OC7"}

print("DEBUG ENV:")
print("API KEY:", os.getenv("AIRTABLE_API_KEY"))
print("BASE ID:", os.getenv("AIRTABLE_BASE_ID"))


# =========================================
# AIRTABLE FETCH
# =========================================

def fetch_airtable_table(table_id, fields=None):

    url = f"https://api.airtable.com/v0/{AIRTABLE_BASE_ID}/{table_id}"

    params = {}

    fields = fields or []

    for i, field in enumerate(fields):
        params[f"fields[{i}]"] = field

    records = []
    offset = None

    while True:

        if offset:
            params["offset"] = offset

        r = requests.get(url, headers=HEADERS, params=params, timeout=30)
        print("Airtable status:", r.status_code)

        if r.status_code != 200:
            print("Airtable error:", r.text)
            return []

        data = r.json()

        records.extend(data.get("records", []))

        offset = data.get("offset")
        if not offset:
            break

    return records


# =========================================
# LOAD SINGLE TABLE
# =========================================

def load_airtable_table(table_id):

    records = fetch_airtable_table(table_id)

    df = pd.json_normalize(records, sep=".")
    df.columns = df.columns.map(lambda x: str(x).replace("fields.", ""))

    df = df.loc[:, ~df.columns.duplicated()]

    return df


# =========================================
# LOAD ALL DATA
# =========================================

def load_all_data():

    tables = {
        "signins": "OT1 Sign-Ins (Workshops)",
        "meetings": "tbl6qMYkcIzkl8q7D",
        "feedback": "Feedback Form Entries",
        "stakeholders": "Stakeholder Reference List",
        "workshops": "Workshop Reference List",
        "ot2": "OT2 Private Sector Engagements",
        "ot4": "OT4 Private Sector Firms",
        "ot5": "OT5 Private Sector Resources",
        "activities": "Master Activity Table",
        "economies": "Economy Reference List",
        "resource_type": "Resource Type",
        "workstreams": "Workstream Reference List",
        "quotes": "Spotlight Quotes",
        "kpis": "KPI Targets",
        "activity_types": "Activity Type Reference Table",
        "fiscal_years": "Fiscal Year",
        "statuses": "Status",
        "speakers": "Speakers",
        "resources": "Resources"
    
    }

    data = {}

    def load_table(key, table_id):
        return key, load_airtable_table(table_id)

    with ThreadPoolExecutor(max_workers=4) as executor:

        futures = [
            executor.submit(load_table, key, table_id)
            for key, table_id in tables.items()
        ]

        for future in futures:
            key, df = future.result()
            data[key] = df

    return data
    
DATASTORE = load_all_data()

blob_service = BlobServiceClient.from_connection_string(
    os.getenv("AZURE_STORAGE_CONNECTION_STRING")
)

container = blob_service.get_container_client("resources")

STORAGE_ACCOUNT = "usapecstorage"
CONTAINER_NAME = "resources"

# =====================================
# AZURE BLOB STORAGE
# =====================================

import os
from datetime import datetime, timedelta

from azure.storage.blob import (
    BlobServiceClient,
    BlobSasPermissions,
    generate_blob_sas,
)

# Connection
connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")

blob_service = BlobServiceClient.from_connection_string(connection_string)

container = blob_service.get_container_client("resources")

# Constants
STORAGE_ACCOUNT = "usapecstorage"
CONTAINER_NAME = "resources"

# Read the account key from the connection string
ACCOUNT_KEY = None

for part in connection_string.split(";"):
    if part.startswith("AccountKey="):
        ACCOUNT_KEY = part.split("=", 1)[1]
        break


# =====================================
# HELPERS
# =====================================

def get_thumbnail_path(blob_path):
    """
    Reports/report.pdf
        ↓
    Thumbnails/report.png
    """

    if not blob_path:
        return None

    filename = os.path.basename(blob_path)
    name, _ = os.path.splitext(filename)

    return f"Thumbnails/{name}.png"


def get_blob_url(blob_path, download=False):

    if not blob_path:
        return None

    disposition = "attachment" if download else "inline"

    sas = generate_blob_sas(
        account_name=STORAGE_ACCOUNT,
        container_name=CONTAINER_NAME,
        blob_name=blob_path,
        account_key=ACCOUNT_KEY,
        permission=BlobSasPermissions(read=True),
        expiry=datetime.utcnow() + timedelta(hours=2),
        content_disposition=disposition,
    )

    return (
        f"https://{STORAGE_ACCOUNT}.blob.core.windows.net/"
        f"{CONTAINER_NAME}/{blob_path}?{sas}"
    )

def get_thumbnail_url(blob_path):
    """
    Converts the PDF path into the thumbnail path and
    returns a SAS URL.
    """

    thumbnail_path = get_thumbnail_path(blob_path)

    return get_blob_url(thumbnail_path)


ACTIVITY_TYPE_ICONS = {
    "Technical Workshop": "bi-people",
    "Policy Dialogue": "bi-chat-dots",
    "Technical Assistance": "bi-tools",
    "Institutional Support": "bi-diagram-3",
    "USG Engagement Support": "bi-flag",
    "Technical Report": "bi-file-earmark-text",
}

sidebar = html.Div(
    [

        html.H4(
            "LIBRARY",
            style={
                "fontSize": "20px",
                "letterSpacing": "1px",
                "color": "#2f4a5f",
                "fontWeight": "600",
                "textTransform": "uppercase"
            }
        ),

        html.Hr(),

        # 🔥 RESOURCE TYPE (PILLS)
        dbc.Nav(
            id="resource-type-sidebar",
            vertical=True,
            pills=True,
            style={"width": "100%"},
        ),

        html.Br(),
        html.Hr(),

        # 🔥 SECTION LABEL
        html.Small("SUITE", className="sidebar-tools-label"),

                # 🔹 DASHBOARD
        html.A(
            [
                html.I(className="bi bi-bar-chart sidebar-icon"),
                html.Span("Dashboard", className="sidebar-export-text")
            ],
            href="https://us-apec-rise-performance-dashboard-f4f7e6hmauggaeew.centralus-01.azurewebsites.net/",
            target="_blank",
            className="sidebar-export-row",
            style={
                "textDecoration": "none",
                "color": "inherit",
            },
        ),
        # 🔹 EXPLORER
        html.Div(
            [
                html.I(className="bi bi-search sidebar-icon"),
                html.Span("Explorer", className="sidebar-export-text")
            ],
            className="sidebar-export-row"
        ),

    ],
    id="sidebar",
    style={
        "width": "230px",
        "position": "fixed",
        "top": "0",
        "left": "0",
        "height": "100vh",
        "backgroundColor": "#e9edf2",
        "padding": "20px",
        "zIndex": "1000"
    }
)

# ==============================
# TOP BAR
# ==============================
topbar = html.Div(
    [
        # Sidebar Toggle
        html.Button(
            "☰",
            id="sidebar-toggle",
            style={
                "background": "none",
                "border": "none",
                "font-size": "20px",
                "color": "#355f7c",
                "cursor": "pointer",
                "margin-right": "15px"
            }
        ),

        # Title + Subtitle Block
        html.Div(
            [
                html.H3(
                    [
                        html.Span(
                            "US APEC-RISE Resource Library",
                            style={
                                "fontSize": "30px",
                                "letterSpacing": "1px",
                                "color": "#2f4a5f",
                                "fontWeight": "600",
                                "textTransform": "uppercase"
                            }),
                       
    
                    ],
                    style={
                        "margin": 0,
                        "font-weight": "600"
                    }
                ),

                html.Div(
                    id="page-subtitle",
                    style={
                        "font-size": "14px",
                        "color": "#355f7c",
                        "font-style": "italic",
                        "margin-top": "4px"
                    }
                ),
            ],
        ),

        # Spacer (pushes timestamp right)
        html.Div(style={"margin-left": "auto"}),

        # Timestamp (Top Right)
        html.Div(
            [
                html.I(className="bi bi-clock me-1"),
                html.Span(id="last-updated"),
            ],
            className="timestamp-container"
        ),
    ],
    className="topbar",
    style={
        "borderBottom": "1px solid #d6dee6"}

)

# =====================================
# MOCK DATA
# =====================================

DATA = [
    {"type": "insight", "text": "Coordination barriers are high in cybersecurity", "workstream": "Cybersecurity", "economy": "Vietnam"},
    {"type": "quote", "text": "Interagency coordination remains difficult", "workstream": "Cybersecurity", "economy": "Vietnam"},
    {"type": "deliverable", "text": "Cybersecurity Workshop Report", "workstream": "Cybersecurity", "economy": "Vietnam", "link": "#"},
]

# ==============================
# DATA FILTER HELPER
# ==============================
def build_filtered_dataframe(economy, workstream, activity, fy, status):

    df = get_activity_data()

    if economy:
        df = df[df["Economy Name"] == economy]

    if workstream:
        df = df[df["Workstream Name"] == workstream]

    if resource_type:
        df = df[df["Resource Type Name"] == resource_type]   

    if activity:
        df = df[df["Activity Type Name"] == activity]

    if fy:
        df = df[df["Fiscal Year Name"] == fy]

    if status:
        df = df[df["Status"] == status]

    return df

# =====================================
# CARD
# =====================================

def build_card(item):

    thumbnail = get_thumbnail_url(item.get("Blob Path"))

    return dbc.Card(

        [

            # -----------------------------
            # Thumbnail
            # -----------------------------
            html.Div(

                html.Img(
                    src=thumbnail,
                    style={
                        "width": "100%",
                        "height": "200px",
                        "objectFit": "contain",
                        "backgroundColor": "#f4f7fb",
                        "borderTopLeftRadius": "12px",
                        "borderTopRightRadius": "12px",
                    },

                ) if thumbnail else html.I(
                    className="bi bi-file-earmark-text"
                ),

                style={
                    "height": "200px",
                    "display": "flex",
                    "alignItems": "center",
                    "justifyContent": "center",
                    "backgroundColor": "#f4f7fb",
                    "fontSize": "28px",
                    "color": "#355f7c",
                    "overflow": "hidden",
                    "borderTopLeftRadius": "12px",
                    "borderTopRightRadius": "12px",
                },
            ),

            # -----------------------------
            # Card Body
            # -----------------------------
            dbc.CardBody(

                [

                    # Title
                    html.Div(
                        item.get("Title", "Untitled Resource"),
                        style={
                            "fontWeight": "600",
                            "fontSize": "14px",
                            "color": "#2f4a5f",
                            "marginBottom": "6px",
                        },
                    ),

                    # Description
                    html.Div(
                        item.get("Description", ""),
                        style={
                            "fontSize": "12px",
                            "color": "#6c7a89",
                            "marginBottom": "10px",
                        },
                    ),

                    # Tags
                    html.Div(

                        [

                            html.Span(
                                item.get("Resource Type Name", ""),
                                className="tag",
                            ),

                            html.Span(
                                item.get("Workstream Name", ""),
                                className="tag",
                            ),

                            html.Span(
                                item.get("Economy Name", ""),
                                className="tag",
                            ),

                            html.Span(
                                item.get("Fiscal Year Name", ""),
                                className="tag",
                            ),

                        ],

                        style={"marginBottom": "12px"},

                    ),

                    html.Hr(style={"margin": "10px 0"}),

dbc.Row(

    [

        dbc.Col(

            dbc.Button(

                [
                    html.I(className="bi bi-eye me-1"),
                    "View",
                ],

                href=get_blob_url(item.get("Blob Path")),
                target="_blank",
                style={
                    "backgroundColor": "#355f7c",
                    "borderColor": "#355f7c",
                    "color": "white",
                    },
                outline=True,
                size="sm",
                className="w-100",

            ),

            width=6,

        ),

        dbc.Col(

            dbc.Button(

                [
                    html.I(className="bi bi-download me-1"),
                    "Download",
                ],

                href=get_blob_url(item.get("Blob Path")),
                style={
                    "backgroundColor": "white",
                    "borderColor": "#355f7c",
                    "color": "#355f7c",
                    },
                outline=True,
                size="sm",
                className="w-100",

            ),

            width=6,

        ),

    ],

    className="g-2",

),

                ]

            ),

        ],

        className="resource-card h-100",

    )

app.layout = html.Div(

    [

        dcc.Location(id="url"),
        dcc.Download(id="download-all"),
        dcc.Store(id="activity-type-counts"),

        dcc.Store(
            id="selected-resource-type",
            data="All Resources"
        ),

        # ================= MAIN APP WRAPPER =================
        html.Div(

            [

                # ================= SIDEBAR =================
                sidebar,

                # ================= MAIN CONTENT =================
                html.Div(

                    [

                        topbar,

                        # ================= FILTER SECTION =================
                        html.Div(

                            [

                                # 🔹 ROW 1: Title + Button
                                html.Div(
                                    [

                                        html.Div(
                                            "FILTER RESOURCES",
                                            className="section-label"
                                        ),

                                        html.Button(
                                            [
                                                html.I(className="bi bi-download me-1"),
                                                "Download All"
                                            ],
                                            id="download-all-btn",
                                            className="download-subtle-btn"
                                        )

                                    ],
                                    style={
                                        "display": "flex",
                                        "justifyContent": "space-between",
                                        "alignItems": "center"
                                    }
                                ),

                                # 🔹 ROW 2: Description
                                html.Div(
                                    "Use filters to quickly find reports, dashboards, and other resources by economy, workstream, activity type, or fiscal year.",
                                    style={
                                        "fontStyle": "italic",
                                        "fontSize": "13px",
                                        "color": "#6c757d",
                                        "marginTop": "4px",
                                        "marginBottom": "8px"
                                    }
                                ),
# 🔹 FILTER CARD
dbc.Card(
    dbc.CardBody(

        [

            # ==========================================
            # SEARCH
            # ==========================================
            html.Div(
                [
                    html.Div(
                        "Search",
                        className="filter-label",
                        style={"marginBottom": "6px"},
                    ),

                    dcc.Input(
                        id="search-input",
                        placeholder="Search resources by title, keyword, or description...",
                        className="search-input",
                        style={
                            "width": "100%",
                            "height": "42px",
                        },
                    ),
                ],
                style={"marginBottom": "20px"},
            ),

            # ==========================================
            # FILTERS - ROW 1
            # ==========================================
            dbc.Row(

                [

                    dbc.Col(
                        [
                            html.Div("Resource", className="filter-label"),
                            dcc.Dropdown(
                                id="resource-type-filter",
                                placeholder="All Resources",
                                clearable=True,
                            ),
                        ],
                        md=4,
                    ),

                    dbc.Col(
                        [
                            html.Div("Economy", className="filter-label"),
                            dcc.Dropdown(
                                id="economy-filter",
                                placeholder="All Economies",
                                clearable=True,
                            ),
                        ],
                        md=4,
                    ),

                    dbc.Col(
                        [
                            html.Div("Workstream", className="filter-label"),
                            dcc.Dropdown(
                                id="workstream-filter",
                                placeholder="All Workstreams",
                                clearable=True,
                            ),
                        ],
                        md=4,
                    ),

                ],

                className="g-3 mb-3",

            ),

            # ==========================================
            # FILTERS - ROW 2
            # ==========================================
            dbc.Row(

                [

                    dbc.Col(
                        [
                            html.Div("Activity", className="filter-label"),
                            dcc.Dropdown(
                                id="activity-filter",
                                placeholder="All Activities",
                                clearable=True,
                            ),
                        ],
                        md=5,
                    ),

                    dbc.Col(
                        [
                            html.Div("Fiscal Year", className="filter-label"),
                            dcc.Dropdown(
                                id="fy-filter",
                                placeholder="All Fiscal Years",
                                clearable=True,
                            ),
                        ],
                        md=4,
                    ),

                    dbc.Col(
                        [
                            html.Div(" ", className="filter-label"),

                            dbc.Button(
                                [
                                    html.I(className="bi bi-x-circle me-2"),
                                    "Clear Filters",
                                ],
                                id="clear-filters",
                                className="button-clear-filters-btn",
                                color="light",
                                n_clicks=0,
                                style={
                                    "width": "100%",
                                    "height": "38px",
                                },
                            ),

                        ],
                        md=3,
                        style={
                            "display": "flex",
                            "flexDirection": "column",
                            "justifyContent": "flex-end",
                        },
                    ),

                ],

                className="g-3 align-items-end",

            ),

        ]

    ),

    className="filter-bar",
    style={
        "marginTop": "10px",
    },

),
# ================= RESULTS =================
html.Div(id="results"),

                            ],

                            style={
                                "marginTop": "24px",
                                "marginBottom": "24px"
                            }

                        ),

                    ],

                    id="main-content",
                    style={
                        "marginLeft": "230px", 
                        "padding": "24px 28px",
                        "backgroundColor": "#f8fafc",
                        "minHeight": "100vh",
                        "width": "100%",
                        "transition": "margin-left 0.3s"
                    }

                )

            ],

            style={
                "display": "flex" 
            }

        )

    ],

    className="app-container"
)

# =====================================
# CALLBACKS
# =====================================

@app.callback(
    Output("results", "children"),
    Input("search-input", "value"),
    Input("economy-filter", "value"),
    Input("workstream-filter", "value"),
    Input("resource-type-filter", "value"),
    Input("activity-filter", "value"),
    Input("fy-filter", "value"),
    Input("clear-filters", "n_clicks"),
    Input("selected-resource-type", "data"),
)
def update_results(
    query,
    economy,
    workstream,
    resource_type,
    activity,
    fy,
    clear_click,
    selected,
):

    df = load_airtable_table("Resources")
    
    # Clean Airtable list fields
    def clean(val):
        return val[0] if isinstance(val, list) else val

    for col in ["Economy", "Workstream", "Activity Type", "Fiscal Year"]:
        if col in df.columns:
            df[col] = df[col].apply(clean)

    # Clear filters
    trigger = ctx.triggered_id

    if trigger == "clear-filters":
        query = None
        economy = None
        workstream = None
        activity = None
        fy = None

    # Apply filters
    if economy:
        df = df[df["Economy"] == economy]

    if workstream:
        df = df[df["Workstream"] == workstream]

    if fy:
        df = df[df["Fiscal Year"] == fy]

    # Activity Type logic
    if activity:
        df = df[df["Activity Type"] == activity]

    elif selected and selected != "All Resources":
        df = df[df["Resource Type Name"] == selected]

    # Search
    if query:
        df = df[df.apply(
            lambda row: query.lower() in str(row).lower(),
            axis=1
        )]

    records = df.to_dict("records")

    # ================= EMPTY STATE =================
    if len(records) == 0:
        return dbc.Container(
            html.Div(
                [
                    html.Div(
                        "No resources match your current filters.",
                        style={
                            "fontSize": "16px",
                            "fontWeight": "600",
                            "color": "#355f7c",
                            "marginBottom": "6px"
                        }
                    ),
                    html.Div(
                        "Try adjusting your filters or search terms.",
                        style={
                            "fontSize": "13px",
                            "color": "#6c7a89"
                        }
                    )
                ],
                style={
                    "textAlign": "center",
                    "padding": "60px 0"
                }
            ),
            fluid=True 
        )

    # ================= RESULTS =================
    return dbc.Container(

        [

            html.Div(
                f"{len(records)} results found",
                style={
                    "fontSize": "13px",
                    "color": "#6c757d",
                    "marginBottom": "10px"
                }
            ),

            dbc.Row(

                [
                    dbc.Col(
                        build_card(r),
                        xs=12, sm=6, md=4, lg=3
                    )
                    for r in records[:20]
                ],

                className="g-4",
                justify="start",  
                style={"width": "100%"}

            )

        ],

        fluid=True
    )

@app.callback(
    Output("last-updated", "children"),
    Input("url", "pathname")
)
def update_timestamp(_):
    now = datetime.now(ZoneInfo("America/New_York"))
    return now.strftime("Library loaded: %d %B %Y • %I:%M %p ET")

@app.callback(
    Output("sidebar", "style"),
    Output("main-content", "style"),
    Input("sidebar-toggle", "n_clicks"),
    State("sidebar", "style"),
    State("main-content", "style"),
    prevent_initial_call=True,
)
def toggle_sidebar(n, sidebar_style, main_style):

    sidebar_style = sidebar_style or {}
    main_style = main_style or {}

    is_open = sidebar_style.get("width", "230px") == "230px"

    if is_open:
        sidebar_style = {
        "width": "0",
        "padding": "0",
        "overflow": "hidden",
        "position": "fixed",
        "left": "0",
        "top": "0",
        "height": "100vh",
        "backgroundColor": "#e9edf2",
        "zIndex": "1000",
    }

        main_style["marginLeft"] = "0"

    else:
        sidebar_style = {
        "width": "230px",
        "padding": "20px",
        "overflow": "visible",
        "position": "fixed",
        "left": "0",
        "top": "0",
        "height": "100vh",
        "backgroundColor": "#e9edf2",
        "zIndex": "1000",
    }

        main_style["marginLeft"] = "230px"

    return sidebar_style, main_style
    
@app.callback(
    Output("activity-type-counts", "data"),
    Input("url", "pathname")
)
def update_counts(_):

    df = load_airtable_table("Resources")
    
    def clean(val):
        return val[0] if isinstance(val, list) else val

    # Use the LOOKUP field, not the linked record ID
    if "Activity Type Name" in df.columns:
        df["Activity Type Name"] = df["Activity Type Name"].apply(clean)

    counts = df["Activity Type Name"].value_counts().to_dict()
    counts["All Resources"] = len(df)

    return counts


@app.callback(
    Output("resource-type-sidebar", "children"),
    Input("activity-type-counts", "data"),
    Input("selected-resource-type", "data"),
)
def render_sidebar(counts, selected):

    if not counts:
        return []

    items = []

    # All Resources
# All Resources
    items.append(
        dbc.NavLink(
            [
                html.I(className="bi bi-folder sidebar-icon"),
                html.Span(" All Resources"),
                html.Span(
                    counts.get("All Resources", 0),
                    style={"marginLeft": "auto", "fontSize": "12px"}
            )
            ],
            id={"type": "resource-filter", "value": "All Resources"}, 
            n_clicks=0,                                             
            href="#",
            active=(selected == "All Resources"),
            style={
                "display": "flex",
                "alignItems": "center",
                "width": "100%",       
                "cursor": "pointer",   
                "padding": "10px 14px"
            }
        )
    )

    # Activity types
    for k, v in counts.items():

        if k == "All Resources":
            continue

        icon = ACTIVITY_TYPE_ICONS.get(k, "bi-file")

        items.append(
            dbc.NavLink(
    [
                html.I(className=f"bi {icon} sidebar-icon"),
                html.Span(f" {k}"),
                html.Span(v, style={"marginLeft": "auto", "fontSize": "12px"})
    ],
            id={"type": "resource-filter", "value": k}, 
            n_clicks=0,
            active=(selected == k),
            style={
                "display": "flex",
                "alignItems": "center",
                "width": "100%",      
                "cursor": "pointer",
                "padding": "10px 14px"
            }
        )
        )

    return items


@app.callback(
    Output("selected-resource-type", "data"),
    Input({"type": "resource-filter", "value": ALL}, "n_clicks"),
    Input("clear-filters", "n_clicks"),
    prevent_initial_call=True
)
def update_selected_resource(n_clicks, clear_click):

    trigger = ctx.triggered_id

    # CLEAR BUTTON CLICKED
    if trigger == "clear-filters":
        return "All Resources"

    # SIDEBAR CLICK
    if isinstance(trigger, dict):
        return trigger["value"]

    return dash.no_update

@app.callback(
    Output("resource-type-filter", "options"),
    Output("economy-filter", "options"),
    Output("workstream-filter", "options"),
    Output("activity-filter", "options"),
    Output("fy-filter", "options"),
    Input("url", "pathname")
)
def populate_filters(_):

    def clean(val):
        return val[0] if isinstance(val, list) else val

    # 🔹 Pull each table separately (your requirement)
    resource_df = DATASTORE["resource_type"].copy()
    print("RESOURCE DF COLUMNS:")
    print(resource_df.columns.tolist())
    print(resource_df.head())   
    econ_df = DATASTORE["economies"].copy()
    ws_df = DATASTORE["workstreams"].copy()
    act_df = DATASTORE["activity_types"].copy()
    fy_df = DATASTORE["fiscal_years"].copy()

    # Clean Airtable fields
    for df in [resource_df, econ_df, ws_df, act_df, fy_df]:
        for col in df.columns:
            df[col] = df[col].apply(clean)

    # BUILD OPTIONS (adjust column names if needed)

    resource_options = [
        {"label": x, "value": x}
        for x in sorted(resource_df["Resource Type"].dropna().unique())
    ]

    econ_options = [
        {"label": x, "value": x}
        for x in sorted(econ_df["Economy"].dropna().unique())
    ]

    ws_options = [
        {"label": x, "value": x}
        for x in sorted(ws_df["Workstream"].dropna().unique())
    ]

    act_options = [
        {"label": x, "value": x}
        for x in sorted(act_df["Activity Type"].dropna().unique())
    ]

    fy_options = [
        {"label": x, "value": x}
        for x in sorted(fy_df["Fiscal Year"].dropna().unique())
    ]

    return  resource_options, econ_options, ws_options, act_options, fy_options

@app.callback(
    Output("download-all", "data"),
    Input("download-all-btn", "n_clicks"),
    State("search-input", "value"),
    State("economy-filter", "value"),
    State("workstream-filter", "value"),
    State("activity-filter", "value"),
    State("fy-filter", "value"),
    prevent_initial_call=True
)

def download_all(n_clicks, query, economy, workstream, activity, fy):

    df = load_airtable_table("Resources")

    # Clean Airtable fields
    def clean(val):
        return val[0] if isinstance(val, list) else val

    for col in [
    "Economy",
    "Workstream Reference List",
    "Activity Type",
    "Fiscal Year",
    "Resource Type",
]:
        if col in df.columns:
            df[col] = df[col].apply(clean)

    # Apply SAME filters as your UI
    if economy:
        df = df[df["Economy"] == economy]

    if workstream:
        df = df[df["Workstream"] == workstream]

    if activity:
        df = df[df["Activity Type"] == activity]

    if fy:
        df = df[df["Fiscal Year"] == fy]

    if query:
        df = df[df.apply(
            lambda row: query.lower() in str(row).lower(),
            axis=1
        )]

    # If nothing selected → don’t download
    if df.empty:
        return None

    # ================= ZIP CREATION =================
    zip_buffer = io.BytesIO()

    with zipfile.ZipFile(zip_buffer, "w") as z:

        for i, row in df.iterrows():

            name = str(row.get("Name", f"resource_{i}")).replace("/", "-")

            resource_type = row.get("Resource Type")

            # ================= FILES (BLOB) =================
            if resource_type == "File":

                url = row.get("Blob URL")

                if url:
                    try:
                        r = requests.get(url)
                        if r.status_code == 200:

                            # try to preserve file extension
                            ext = url.split(".")[-1].split("?")[0]
                            filename = f"{name}.{ext}"

                            z.writestr(filename, r.content)

                    except Exception as e:
                        print(f"Error downloading {name}: {e}")

            # ================= DASHBOARDS / LINKS =================
            else:

                url = row.get("URL", "")

                z.writestr(
                    f"{name}.txt",
                    f"{name}\n{url}"
                )

    zip_buffer.seek(0)

    return dcc.send_bytes(zip_buffer.getvalue(), "Filtered_Resources.zip")
    

@app.callback(
    Output("search-input", "value"),
    Output("economy-filter", "value"),
    Output("workstream-filter", "value"),
    Output("activity-filter", "value"),
    Output("fy-filter", "value"),
    Input("clear-filters", "n_clicks"),
    prevent_initial_call=True
)
def clear_all_filters(n):
    return None, None, None, None, None

# =====================================
# RUN
# =====================================

if __name__ == "__main__":
    app.run(debug=True, port=8051)
