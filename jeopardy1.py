"""
Jeopardy Gameboard for Discord (Python + Streamlit)
--------------------------------------------------

Quick start
1) Open the terminal and bash:  pip install streamlit==1.39.0
2) Run on the terminal using:      streamlit run jeopardy1.py --server.maxUploadSize=1024
3) Share your screen in Discord and play!

Features
- Add/remove categories (columns)
- Create custom questions & answers
- Attach media to questions (images, video, audio) via upload or URL
- Adjustable point values per question
- Rename categories, edit questions
- Mark questions as used; reveal answers
- Save/Load entire board (JSON with embedded media)
- Simple team scoreboard (add teams, adjust points)

Notes
- This is a single-file app. No database needed. Your board saves to a downloadable JSON file.
- Media uploaded is embedded (base64) inside the saved JSON so you can share a single file.
- Works offline once installed.
"""

import base64
import io
import json
import uuid
import time
from dataclasses import dataclass, asdict
from typing import Dict, List, Optional, Tuple

import streamlit as st

# ----------------------------- Data Models -----------------------------
@dataclass
class MediaItem:
    kind: str  # 'image' | 'video' | 'audio'
    source: str  # 'upload' | 'url'
    filename: Optional[str] = None
    mime: Optional[str] = None
    b64: Optional[str] = None  # base64-encoded bytes when source=='upload'
    url: Optional[str] = None  # when source=='url'

    def to_display(self) -> Tuple[str, Optional[bytes], Optional[str]]:
        """Return (kind, bytes_or_None, url_or_None) for display in Streamlit."""
        if self.source == 'upload' and self.b64:
            return self.kind, base64.b64decode(self.b64), None
        return self.kind, None, self.url


@dataclass
class Question:
    id: str
    points: int
    prompt: str
    answer: str
    media: List[MediaItem]
    timer_seconds: int = 30  # countdown length in seconds (default)


@dataclass
class Category:
    id: str
    name: str
    questions: List[Question]


@dataclass
class Board:
    title: str
    categories: List[Category]


# ----------------------------- Helpers -----------------------------
def new_board() -> Board:
    return Board(title="Jeopardy!", categories=[])


def ensure_state():
    if 'board' not in st.session_state:
        st.session_state.board = new_board()
    if 'used_qids' not in st.session_state:
        st.session_state.used_qids = set()  # set of question.id
    if 'active_qid' not in st.session_state:
        st.session_state.active_qid = None
    if 'reveal_answer' not in st.session_state:
        st.session_state.reveal_answer = False
    if 'scores' not in st.session_state:
        st.session_state.scores = {}  # team_name -> int
    if 'team_colors' not in st.session_state:
        st.session_state.team_colors = {}  # team_name -> hex color
    # track last processed upload to avoid re-processing across reruns
    if 'upload_board_last' not in st.session_state:
        st.session_state.upload_board_last = None


def serialize_board(board: Board) -> str:
    # Convert dataclasses to plain Python dicts first (handles nested dataclasses).
    return json.dumps(asdict(board), indent=2)


def deserialize_board(s: str) -> Board:
    raw = json.loads(s)

    def to_media(m):
        return MediaItem(
            kind=m['kind'], source=m['source'], filename=m.get('filename'),
            mime=m.get('mime'), b64=m.get('b64'), url=m.get('url')
        )

    def to_q(q):
        return Question(
            id=q['id'], points=int(q['points']), prompt=q['prompt'],
            answer=q['answer'], media=[to_media(m) for m in q.get('media', [])],
            timer_seconds=int(q.get('timer_seconds', 30))
        )

    def to_cat(c):
        return Category(id=c['id'], name=c['name'], questions=[to_q(q) for q in c.get('questions', [])])

    return Board(title=raw.get('title', 'Jeopardy!'), categories=[to_cat(c) for c in raw.get('categories', [])])


def find_question(board: Board, qid: str) -> Optional[Tuple[Category, Question]]:
    for cat in board.categories:
        for q in cat.questions:
            if q.id == qid:
                return cat, q
    return None


def category_by_id(board: Board, cid: str) -> Optional[Category]:
    for c in board.categories:
        if c.id == cid:
            return c
    return None


# ----------------------------- UI: Sidebar (Editor & Save/Load) -----------------------------

def sidebar_editor(board: Board):
    st.sidebar.header("Board Editor")

    # Board title
    st.sidebar.text_input("Game Title", value=board.title, key="board_title_input")
    # keep board.title in sync with the text input (use get so first run is safe)
    board.title = st.session_state.get("board_title_input", board.title)

    with st.sidebar.expander("Categories", expanded=True):
        # Add category
        new_cat_name = st.text_input("New category name", key="new_cat_name")
        cols = st.columns([1, 0.4])

        # callbacks so we don't assign to session_state keys after the widget is created
        def _add_category(b):
            name = st.session_state.get("new_cat_name", "").strip()
            if name:
                b.categories.append(Category(id=str(uuid.uuid4()), name=name, questions=[]))
                st.session_state["new_cat_name"] = ""
            # st.rerun() removed — Streamlit will rerun after the callback finishes.

        def _clear_new_cat():
            st.session_state["new_cat_name"] = ""
            # no st.rerun() needed
        with cols[0]:
            st.button("Add Category", use_container_width=True, key="add_category_btn", on_click=_add_category, args=(board,))
        with cols[1]:
            st.button("Clear Name", use_container_width=True, key="clear_new_cat_name_btn", on_click=_clear_new_cat)

        # Existing categories list with rename/remove
        if board.categories:
            for cat in board.categories:
                c1, c2 = st.columns([0.7, 0.3])
                with c1:
                    new_name = st.text_input(f"Rename '{cat.name}'", value=cat.name, key=f"rename_{cat.id}")
                    cat.name = new_name
                with c2:
                    if st.button("Remove", key=f"rm_{cat.id}"):
                        # Remove category and any active/used references
                        st.session_state.used_qids = {qid for qid in st.session_state.used_qids
                                                      if find_question(board, qid) is not None}
                        board.categories = [c for c in board.categories if c.id != cat.id]
                        st.rerun()

    with st.sidebar.expander("Add Question", expanded=True):
        # Choose category
        if board.categories:
            # Use a stable mapping from category id -> label for selectbox options.
            label_by_id = {c.id: f"{i+1}. {c.name}" for i, c in enumerate(board.categories)}
            cat_ids = list(label_by_id.keys())
            cat_choice = st.selectbox("Category", options=cat_ids, format_func=lambda cid: label_by_id[cid])
            chosen_cid = cat_choice
            points = st.number_input("Points", min_value=0, max_value=5000, step=100, value=200, key="add_points")
            prompt = st.text_area("Question/Prompt")
            answer = st.text_area("Answer (hidden during play)")
            timer_for_new = st.number_input("Timer (seconds)", min_value=5, max_value=600, value=30, step=5, key="add_question_timer")

            st.markdown("**Attach media (optional)**")
            img_files = st.file_uploader("Images", type=["png", "jpg", "jpeg", "gif"], accept_multiple_files=True)
            vid_file = st.file_uploader("Video (mp4/mov/webm)", type=["mp4", "mov", "webm"], accept_multiple_files=False)
            aud_file = st.file_uploader("Audio (mp3/wav/ogg)", type=["mp3", "wav", "ogg"], accept_multiple_files=False)

            st.markdown("— or link —")
            img_url = st.text_input("Image URL")
            vid_url = st.text_input("Video URL")
            aud_url = st.text_input("Audio URL")

            if st.button("Add Question to Category", type="primary"):
                media: List[MediaItem] = []
                # Uploaded images
                for f in img_files or []:
                    media.append(MediaItem(
                        kind='image', source='upload', filename=f.name, mime=f.type,
                        b64=base64.b64encode(f.read()).decode('utf-8')
                    ))
                # Uploaded video
                if vid_file is not None:
                    media.append(MediaItem(
                        kind='video', source='upload', filename=vid_file.name, mime=vid_file.type,
                        b64=base64.b64encode(vid_file.read()).decode('utf-8')
                    ))
                # Uploaded audio
                if aud_file is not None:
                    media.append(MediaItem(
                        kind='audio', source='upload', filename=aud_file.name, mime=aud_file.type,
                        b64=base64.b64encode(aud_file.read()).decode('utf-8')
                    ))
                # URLs
                if img_url.strip():
                    media.append(MediaItem(kind='image', source='url', url=img_url.strip()))
                if vid_url.strip():
                    media.append(MediaItem(kind='video', source='url', url=vid_url.strip()))
                if aud_url.strip():
                    media.append(MediaItem(kind='audio', source='url', url=aud_url.strip()))

                q = Question(id=str(uuid.uuid4()), points=int(points), prompt=prompt, answer=answer, media=media,
                             timer_seconds=int(timer_for_new))
                category_by_id(board, chosen_cid).questions.append(q)
                st.success("Question added!")
        else:
            st.info("Create a category first.")

    with st.sidebar.expander("Edit Question", expanded=False):
        # Select question to edit
        # Build a mapping qid -> label and present qid list to selectbox to avoid tuple-state issues.
        label_by_qid = {}
        for c in board.categories:
            for q in c.questions:
                label_by_qid[q.id] = f"{c.name} – {q.points}"
        if label_by_qid:
            qid = st.selectbox("Choose question", options=list(label_by_qid.keys()), format_func=lambda qid: label_by_qid[qid])
            found = find_question(board, qid)
            if found:
                cat, q = found
                new_points = st.number_input("Points", min_value=0, max_value=5000, step=100, value=q.points, key=f"edit_points_{q.id}")
                new_prompt = st.text_area("Question/Prompt", value=q.prompt)
                new_answer = st.text_area("Answer", value=q.answer)
                new_timer = st.number_input("Timer (seconds)", min_value=5, max_value=600, value=q.timer_seconds, step=5, key=f"edit_timer_{q.id}")

                if st.button("Apply Changes"):
                    q.points = int(new_points)
                    q.prompt = new_prompt
                    q.answer = new_answer
                    q.timer_seconds = int(new_timer)
                    st.success("Updated.")

            if st.button("Delete Question", type="secondary", key=f"delete_q_{q.id}"):
                cat.questions = [qq for qq in cat.questions if qq.id != q.id]
                st.session_state.used_qids.discard(q.id)
                st.success("Deleted.")
                st.rerun()
        else:
            st.info("No questions yet.")

    with st.sidebar.expander("Save / Load Board", expanded=False):
        st.write("Save your board to a file (download) or load a saved board.")
        # Download current board as JSON
        try:
            json_str = serialize_board(board)
        except Exception as e:
            json_str = ""
            st.error(f"Serialization error: {e}")

        st.download_button(
            "Download board (.json)",
            data=json_str,
            file_name="jeopardy_board.json",
            mime="application/json",
            key="download_board_btn",
        )

        # Upload a board JSON to load
        uploaded = st.file_uploader("Load board (.json)", type=["json"], key="upload_board")
        if uploaded is not None:
            try:
                # Create a small stable id for this upload (name + size)
                bytes_data = uploaded.read()
                upload_id = f"{getattr(uploaded,'name', '')}:{len(bytes_data)}"
                # Only process if we haven't already processed this exact upload
                if st.session_state.get("upload_board_last") != upload_id:
                    s = bytes_data.decode("utf-8") if isinstance(bytes_data, (bytes, bytearray)) else str(bytes_data)
                    newb = deserialize_board(s)
                    st.session_state["board"] = newb
                    st.session_state["upload_board_last"] = upload_id
                    st.success("Board loaded.")
                # Do NOT call st.rerun() here — avoids infinite rerun loop while the uploader still holds the file.
            except Exception as e:
                st.error(f"Failed to load board: {e}")

        # Optionally save a copy on the local server (useful when running Streamlit locally)
        autosave_default = st.session_state.get("autosave_to_disk", False)
        autosave = st.checkbox("Autosave board to disk (jeopardy_board_autosave.json)", value=autosave_default, key="autosave_toggle")
        st.session_state["autosave_to_disk"] = autosave

        if st.button("Save board to server file (jeopardy_board_autosave.json)", key="save_to_disk_btn"):
            try:
                from pathlib import Path
                out_path = Path.cwd() / "jeopardy_board_autosave.json"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(serialize_board(board))
                st.success(f"Saved board to {out_path}")
            except Exception as e:
                st.error(f"Failed to write file: {e}")

        # Perform an immediate autosave when toggle enabled
        if autosave:
            try:
                from pathlib import Path
                out_path = Path.cwd() / "jeopardy_board_autosave.json"
                with open(out_path, "w", encoding="utf-8") as f:
                    f.write(serialize_board(board))
                st.info(f"Autosaved to {out_path}")
            except Exception as e:
                st.error(f"Autosave failed: {e}")


# ----------------------------- Score helpers & UI -----------------------------

def _safe_team_key(team: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in team)


def inc_score(team: str, delta: int):
    if team in st.session_state.scores:
        st.session_state.scores[team] += delta


def remove_team(team: str):
    if team in st.session_state.scores:
        del st.session_state.scores[team]
        st.session_state.team_colors.pop(team, None)


def sidebar_scoreboard():
    """Keep only the small team-creation controls in the sidebar."""
    st.sidebar.header("Scoreboard")
    team_name = st.sidebar.text_input("New team name", key="new_team_name")
    team_color = st.sidebar.color_picker("Team color", value="#4CAF50", key="new_team_color")

    def _add_team():
        name = st.session_state.get("new_team_name", "").strip()
        color = st.session_state.get("new_team_color", "#4CAF50")
        if name and name not in st.session_state.scores:
            st.session_state.scores.setdefault(name, 0)
            st.session_state.team_colors.setdefault(name, color)
            st.session_state["new_team_name"] = ""
            # keep color picker value so user can pick another color if they like
            st.session_state["new_team_color"] = color

    def _clear_team_name():
        st.session_state["new_team_name"] = ""

    add_cols = st.sidebar.columns([1, 1])
    with add_cols[0]:
        st.button("Add Team", use_container_width=True, key="add_team_btn", on_click=_add_team)
    with add_cols[1]:
        st.button("Clear Name", use_container_width=True, key="clear_team_name_btn", on_click=_clear_team_name)


def render_scoreboard_main():
    """Render a centered, large scoreboard in the main area (below the board)."""
    if not st.session_state.scores:
        return

    st.markdown("---")
    st.markdown("<div style='text-align:center'><h2>Scoreboard</h2></div>", unsafe_allow_html=True)

    # center the scoreboard content
    left, center, right = st.columns([1, 6, 1])
    with center:
        # CSS to make names and scores large and spaced
        st.markdown(
            """
            <style>
            .score-card { display:flex; align-items:center; justify-content:space-between;
                          background: rgba(255,255,255,0.03); padding:12px; margin:8px 0; border-radius:8px; }
            .score-name { font-size:20px; font-weight:700; }
            .score-value { font-size:28px; font-weight:900; margin-right:18px; }
            .score-buttons > div { padding:2px; }
            </style>
            """,
            unsafe_allow_html=True,
        )

        # Render each team as a horizontal row with big score and control buttons
        for team, score in list(st.session_state.scores.items()):
            safe = _safe_team_key(team)
            color = st.session_state.team_colors.get(team, "#4CAF50")
            cols = st.columns([3, 1, 1, 1])  # name/score / + / - / remove
            with cols[0]:
                # show a colored left border for the team
                st.markdown(f"<div class='score-card' style='border-left:8px solid {color};'><div class='score-name'>{team}</div><div class='score-value'>{score}</div></div>", unsafe_allow_html=True)
            with cols[1]:
                st.button("+100", key=f"main_inc_{safe}", use_container_width=True, on_click=inc_score, args=(team, 100))
            with cols[2]:
                st.button("-100", key=f"main_dec_{safe}", use_container_width=True, on_click=inc_score, args=(team, -100))
            with cols[3]:
                st.button("Remove", key=f"main_rm_{safe}", use_container_width=True, on_click=remove_team, args=(team,))


# ----------------------------- UI: Gameboard -----------------------------

def render_board(board: Board):
    # Inject updated CSS for larger boxed title, slightly bigger category headers,
    # and more spacing between categories / question buttons
    st.markdown(
        """
        <style>
        /* Boxed, larger title centered above the board */
        .game-title-box {
            max-width: 900px;
            margin: 18px auto 32px auto;
            padding: 18px 28px;
            border-radius: 12px;
            background: rgba(255,255,255,0.02);
            border: 1px solid rgba(255,255,255,0.03);
            box-shadow: 0 6px 18px rgba(0,0,0,0.25);
        }
        .game-title-box h1 {
            margin: 0;
            font-size: 48px;
            font-weight: 800;
            text-align: center;
        }

        /* Slightly larger category headers and extra spacing */
        .jeopardy-category {
            text-align: center;
            font-size: 22px;
            font-weight: 800;
            margin-bottom: 16px;
            padding: 6px 10px;
            display: block;
        }

        /* Make question buttons larger and add horizontal spacing between them */
        div.stButton > button {
            font-size: 20px;
            padding: 18px 22px;
            border-radius: 10px;
            min-width: 160px;
            margin: 8px 12px; /* gives extra gap between category columns/buttons */
        }

        /* Ensure the row spacing between category header and first button is a bit larger */
        .jeopardy-column-gap {
            margin-bottom: 18px;
        }

        /* Slight tweak for the centered scoreboard area */
        .score-card { display:flex; align-items:center; justify-content:space-between;
                      background: rgba(255,255,255,0.02); padding:12px; margin:12px 0; border-radius:8px; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    # Centered boxed title
    st.markdown(f"<div class='game-title-box'><h1>{board.title}</h1></div>", unsafe_allow_html=True)

    if not board.categories:
        st.info("Add categories and questions in the sidebar to get started.")
        return

    # Render the board centered in the page (spacers left/right)
    left, center, right = st.columns([1, 6, 1])
    with center:
        # Header row: category names
        cat_cols = st.columns(len(board.categories))
        for i, cat in enumerate(board.categories):
            with cat_cols[i]:
                st.markdown(f"<div class='jeopardy-category'>{cat.name}</div>", unsafe_allow_html=True)

        # Sort questions within each category by points (descending typical Jeopardy style)
        sorted_qs = [sorted(c.questions, key=lambda q: q.points, reverse=True) for c in board.categories]

        # Render grid of buttons (larger, centered) with a small extra gap row class around them
        for row in range(max((len(qs) for qs in sorted_qs), default=0)):
            row_cols = st.columns(len(board.categories))
            for c_idx, qs in enumerate(sorted_qs):
                with row_cols[c_idx]:
                    st.markdown("<div class='jeopardy-column-gap'>", unsafe_allow_html=True)
                    if row < len(qs):
                        q = qs[row]
                        used = q.id in st.session_state.used_qids
                        label = f"{q.points}"
                        # make buttons fill their column (use_container_width still helpful)
                        if used:
                            st.button(label, key=f"used_{q.id}", disabled=True, use_container_width=True)
                        else:
                            if st.button(label, key=f"btn_{q.id}", use_container_width=True):
                                st.session_state.active_qid = q.id
                                st.session_state.reveal_answer = False
                    else:
                        st.write("")
                    st.markdown("</div>", unsafe_allow_html=True)

    # Active question viewer (unchanged, but keep centered by using main area)
    if st.session_state.active_qid:
        sel = find_question(board, st.session_state.active_qid)
        if sel:
            cat, q = sel
            st.markdown("---")
            st.markdown(f"### {cat.name} – **{q.points}**")
            st.markdown(f"**Question:** {q.prompt}")
            # Timer controls
            tcol1, tcol2 = st.columns([1, 1])
            with tcol1:
                # allow per-question timer adjustments before starting
                timer_val = st.number_input("Timer (seconds)", min_value=5, max_value=600, value=q.timer_seconds, key=f"timer_setting_{q.id}")
            with tcol2:
                start_pressed = st.button("Start Timer", key=f"start_timer_{q.id}")
            # placeholder for countdown display
            timer_ph = st.empty()
            if start_pressed:
                # store chosen timer to the question (so it serializes if saved later)
                q.timer_seconds = int(timer_val)
                # blocking countdown loop (updates the placeholder each second)
                for remaining in range(q.timer_seconds, -1, -1):
                    mins, secs = divmod(remaining, 60)
                    timer_ph.markdown(f"### Time remaining: {mins:02d}:{secs:02d}")
                    time.sleep(1)
                # time's up -> mark used and close viewer
                st.session_state.used_qids.add(q.id)
                st.session_state.active_qid = None
                st.session_state.reveal_answer = False
                st.success("Time's up — question marked used.")
                # short pause so user sees the success message
                time.sleep(0.5)

            # Media display
            if q.media:
                st.markdown("**Media:**")
                for idx, m in enumerate(q.media, start=1):
                    kind, data_bytes, url = m.to_display()
                    if kind == 'image':
                        if data_bytes is not None:
                            st.image(data_bytes, caption=m.filename or f"Image {idx}")
                        elif url:
                            st.image(url, caption=url)
                    elif kind == 'video':
                        if data_bytes is not None:
                            st.video(data_bytes)
                        elif url:
                            st.video(url)
                    elif kind == 'audio':
                        if data_bytes is not None:
                            st.audio(data_bytes, format=m.mime or 'audio/mpeg')
                        elif url:
                            st.audio(url)

            # Reveal answer controls
            cols = st.columns([0.25, 0.25, 0.25, 0.25])
            with cols[0]:
                if st.button("Reveal Answer", type="primary", key=f"reveal_{q.id}"):
                    st.session_state.reveal_answer = True
            with cols[1]:
                if st.button("Hide Answer", key=f"hide_{q.id}"):
                    st.session_state.reveal_answer = False
            with cols[2]:
                if st.button("Mark Used", key=f"mark_used_{q.id}"):
                    st.session_state.used_qids.add(q.id)
                    st.session_state.active_qid = None
                    st.session_state.reveal_answer = False
            with cols[3]:
                if st.button("Close", key=f"close_{q.id}"):
                    st.session_state.active_qid = None
                    st.session_state.reveal_answer = False

            if st.session_state.reveal_answer:
                st.success(f"**Answer:** {q.answer}")

    # Bottom controls
    st.markdown("---")
    # Center the Reset / Reveal controls under the board
    b_left, b_center, b_right = st.columns([1, 6, 1])
    with b_center:
        btn_cols = st.columns([0.5, 0.5])
        with btn_cols[0]:
            if st.button("Reset Used Questions", key="reset_used_btn", use_container_width=True):
                st.session_state.used_qids = set()
                st.session_state.active_qid = None
                st.session_state.reveal_answer = False
        with btn_cols[1]:
            if st.button("Reveal All Answers (toggle)", key="toggle_reveal_all", use_container_width=True):
                st.session_state.reveal_answer = not st.session_state.get('reveal_answer', False)


def main():
    st.set_page_config(page_title="Jeopardy Game", layout="wide")
    ensure_state()

    # Editor controls in the sidebar
    sidebar_editor(st.session_state.board)
    sidebar_scoreboard()

    # Render main board and centered scoreboard
    render_board(st.session_state.board)
    try:
        # render_scoreboard_main exists in this file
        render_scoreboard_main()
    except Exception:
        # fail gracefully if scoreboard isn't available for any reason
        pass


if __name__ == "__main__":
    main()
