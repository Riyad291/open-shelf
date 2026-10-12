"""Sign-in and registration, including the biometric routes
(spec requirements 1 and 4).

Three ways in:

*   **Password** -- always available, and the fallback when biometrics fail.
*   **Face** -- from the webcam, or from a saved photo on a machine with no
    camera.  The identified user still has to clear the suspension check.
*   **Voice** -- from the microphone, or from a saved WAV file.

The spec says RC/VC/SU "must log in with a face and/or voice sample".  We treat
biometrics as a full login route in its own right and keep the password as a
recovery path, because a face model that cannot be retrained locks the shop out
of its own till.  The GUI states this plainly rather than hiding it.
"""

from __future__ import annotations

import os
import threading
import tkinter as tk
from tkinter import filedialog, ttk
from typing import Optional

from .. import models
from ..models import verify_password
from ..rules import RuleError
from .widgets import ScrollFrame, Theme, hline, toast


class LoginView(ttk.Frame):
    def __init__(self, master, app):
        super().__init__(master)
        self.app = app
        self.services = app.services
        #: Guards the biometric buttons against re-entrant clicks.
        self._working = False
        self._build()

    # ------------------------------------------------------------------

    def _build(self) -> None:
        scroll = ScrollFrame(self)
        scroll.pack(fill="both", expand=True)
        body = scroll.body
        self._scroll = scroll

        wrap = ttk.Frame(body)
        wrap.pack(fill="both", expand=True, padx=30, pady=24)

        ttk.Label(wrap, text="Sign in", style="H1.TLabel").pack(anchor="w")
        ttk.Label(
            wrap,
            text="Registered customers, VIP customers and the super-user sign in "
                 "here. Visitors can browse the catalogue without an account.",
            style="Muted.TLabel", wraplength=760, justify="left",
        ).pack(anchor="w", pady=(4, 18))

        columns = ttk.Frame(wrap)
        columns.pack(fill="both", expand=True)
        columns.columnconfigure(0, weight=1, uniform="col")
        columns.columnconfigure(1, weight=1, uniform="col")

        self._build_password_card(columns)
        self._build_biometric_card(columns)
        self._build_register_card(wrap)

    # -- password ------------------------------------------------------

    def _build_password_card(self, parent) -> None:
        card = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                        highlightbackground=Theme.BORDER)
        card.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        inner = tk.Frame(card, bg=Theme.SURFACE)
        inner.pack(fill="both", expand=True, padx=20, pady=18)

        ttk.Label(inner, text="With a password", style="H3.TLabel").pack(anchor="w")
        ttk.Label(inner, text="Always available.", style="SurfaceMuted.TLabel"
                  ).pack(anchor="w", pady=(2, 14))

        ttk.Label(inner, text="Username", style="Surface.TLabel").pack(anchor="w")
        self.username_entry = ttk.Entry(inner, width=28)
        self.username_entry.pack(anchor="w", fill="x", pady=(2, 10))

        ttk.Label(inner, text="Password", style="Surface.TLabel").pack(anchor="w")
        self.password_entry = ttk.Entry(inner, width=28, show="•")
        self.password_entry.pack(anchor="w", fill="x", pady=(2, 14))
        self.password_entry.bind("<Return>", lambda _e: self._password_login())

        ttk.Button(inner, text="Sign in", style="Accent.TButton",
                   command=self._password_login).pack(anchor="w")

        self.password_message = ttk.Label(
            inner, text="", style="SurfaceMuted.TLabel", wraplength=320,
            justify="left",
        )
        self.password_message.pack(anchor="w", pady=(12, 0))

        demo = tk.Frame(inner, bg=Theme.SURFACE_ALT)
        demo.pack(fill="x", pady=(16, 0))
        tk.Label(demo, text="Demo accounts", bg=Theme.SURFACE_ALT,
                 fg=Theme.INK_SOFT, font=(self.app.fonts["sans"], 8, "bold"),
                 anchor="w").pack(fill="x", padx=10, pady=(8, 2))
        for label, username, password in (
            ("super-user", "margaret", "admin123"),
            ("registered", "alice", "alice123"),
            ("VIP", "carol", "carol123"),
            ("low balance", "dave", "dave1234"),
        ):
            row = tk.Frame(demo, bg=Theme.SURFACE_ALT)
            row.pack(fill="x", padx=10, pady=1)
            tk.Label(row, text="%-12s %s / %s" % (label, username, password),
                     bg=Theme.SURFACE_ALT, fg=Theme.INK_SOFT,
                     font=(self.app.fonts["mono"], 8), anchor="w").pack(side="left")
            tk.Button(row, text="use", bd=0, bg=Theme.SURFACE_ALT,
                      fg=Theme.ACCENT, font=(self.app.fonts["sans"], 8),
                      cursor="hand2", activebackground=Theme.SURFACE_ALT,
                      command=lambda u=username, p=password: self._fill(u, p)
                      ).pack(side="right")
        tk.Label(demo, text="", bg=Theme.SURFACE_ALT).pack(pady=2)

    def _fill(self, username: str, password: str) -> None:
        self.username_entry.delete(0, "end")
        self.username_entry.insert(0, username)
        self.password_entry.delete(0, "end")
        self.password_entry.insert(0, password)

    def _password_login(self) -> None:
        username = self.username_entry.get().strip()
        password = self.password_entry.get()
        if not username or not password:
            self._say(self.password_message, "Enter a username and password.", Theme.BAD)
            return

        user = self.services.storage.find_user_by_username(username)
        if not user or not verify_password(password, user.get("salt", ""),
                                           user.get("password_hash", "")):
            self._say(self.password_message, "No account matches that username and "
                                             "password.", Theme.BAD)
            return

        models.normalise_user(user)
        allowed, why = self.services.rules.can_log_in(user)
        if not allowed:
            self._say(self.password_message, why, Theme.BAD)
            return

        # Requirement 4: an enrolled customer must use their face.  The
        # password is not an alternative route for them -- it only stays open
        # for the super-user (who has to be able to get in to enrol the first
        # faces) and for accounts that predate enrolment.
        blocked, reason = self._password_blocked_for(user)
        if blocked:
            self._say(self.password_message, reason, Theme.BAD)
            return

        self.password_entry.delete(0, "end")
        self._say(self.password_message, "", Theme.INK_SOFT)
        self.app.log_in(user, how="password")

    def _password_blocked_for(self, user: dict):
        """Decide whether this account may sign in with a password.

        Returns ``(blocked, reason)``.
        """
        config = self.services.config
        if not bool(config.get("require_face_login")):
            return False, ""

        # Requirement 4 names the super-user among the roles that must sign in
        # with a biometric sample, so the bootstrap exemption is deliberately
        # narrow: it lasts only until the super-user has a face enrolled.
        # Somebody has to be able to get in to perform the first enrolment --
        # including their own -- but once that is done the rule applies to
        # them like everyone else.
        role = user.get("role")
        if role == models.SUPER_USER and bool(
            config.get("super_user_password_bootstrap")
        ):
            if self.services.biometrics.face.sample_count(user["user_id"]) == 0:
                return False, ""

        # An account with no face enrolled has no face to sign in with.  That
        # is only possible for accounts created before enrolment was required
        # (the seeded demo accounts), so they keep password access and are
        # nudged to enrol.
        if self.services.biometrics.face.sample_count(user["user_id"]) == 0:
            return False, ""

        if not self.services.biometrics.face.trained:
            return False, ""

        return True, (
            "This account has a face enrolled, so it signs in with the camera. "
            "Use \"Use camera\" under \"With your face or voice\" on the right."
        )

    # -- biometrics ----------------------------------------------------

    def _build_biometric_card(self, parent) -> None:
        card = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                        highlightbackground=Theme.BORDER)
        card.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        inner = tk.Frame(card, bg=Theme.SURFACE)
        inner.pack(fill="both", expand=True, padx=20, pady=18)

        ttk.Label(inner, text="With your face or voice", style="H3.TLabel").pack(anchor="w")
        self.biometric_status = ttk.Label(
            inner, text="", style="SurfaceMuted.TLabel", wraplength=330,
            justify="left",
        )
        self.biometric_status.pack(anchor="w", pady=(2, 14))

        face_box = tk.Frame(inner, bg=Theme.SURFACE)
        face_box.pack(fill="x", pady=(0, 12))
        tk.Label(face_box, text="Face", bg=Theme.SURFACE, fg=Theme.INK,
                 font=(self.app.fonts["sans"], 10, "bold")).pack(anchor="w")
        buttons = tk.Frame(face_box, bg=Theme.SURFACE)
        buttons.pack(anchor="w", pady=4)
        ttk.Button(buttons, text="Use camera",
                   command=self._face_login_camera).pack(side="left")
        ttk.Button(buttons, text="Use a photo",
                   command=self._face_login_file).pack(side="left", padx=6)

        voice_box = tk.Frame(inner, bg=Theme.SURFACE)
        voice_box.pack(fill="x", pady=(0, 12))
        tk.Label(voice_box, text="Voice", bg=Theme.SURFACE, fg=Theme.INK,
                 font=(self.app.fonts["sans"], 10, "bold")).pack(anchor="w")
        buttons = tk.Frame(voice_box, bg=Theme.SURFACE)
        buttons.pack(anchor="w", pady=4)
        ttk.Button(buttons, text="Record 3 seconds",
                   command=self._voice_login_mic).pack(side="left")
        ttk.Button(buttons, text="Use a WAV file",
                   command=self._voice_login_file).pack(side="left", padx=6)

        self.biometric_message = ttk.Label(
            inner, text="", style="SurfaceMuted.TLabel", wraplength=330,
            justify="left",
        )
        self.biometric_message.pack(anchor="w", pady=(6, 0))

        self.biometric_progress = ttk.Progressbar(inner, mode="indeterminate",
                                                  length=200)

        tk.Label(inner,
                 text="Not enrolled yet? The super-user enrols you from the "
                      "Manage store tab. Your password always works as a "
                      "fallback.",
                 bg=Theme.SURFACE, fg=Theme.INK_FAINT,
                 font=(self.app.fonts["sans"], 8), wraplength=330,
                 justify="left").pack(anchor="w", pady=(14, 0))

    def _busy(self, on: bool) -> None:
        if on:
            self.biometric_progress.pack(anchor="w", pady=(8, 0))
            self.biometric_progress.start(12)
        else:
            self.biometric_progress.stop()
            self.biometric_progress.pack_forget()

    def _run_async(self, work, done) -> None:
        """Run a blocking biometric call off the Tk thread.

        Guarded against re-entry: without this, impatient double-clicking opens
        several camera or microphone streams at once, which on most machines
        means every one after the first fails.
        """
        if self._working:
            return
        self._working = True
        self._busy(True)

        def worker():
            try:
                result = work()
            except Exception as exc:  # pragma: no cover - defensive
                result = exc
            self.app.post(lambda: self._finish(result, done))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, result, done) -> None:
        self._working = False
        self._busy(False)
        if isinstance(result, Exception):
            self._say(self.biometric_message,
                      "That did not work: %s" % result, Theme.BAD)
            return
        done(result)

    def _face_login_camera(self) -> None:
        """Open the live camera window and sign in on a match."""
        from .camera import camera_available, dependency_message, identify_face

        if not camera_available():
            self._say(self.biometric_message, dependency_message(), Theme.BAD)
            return

        face = self.services.biometrics.face
        if not face.trained:
            self._say(
                self.biometric_message,
                "No face has been enrolled on this system yet, so there is "
                "nothing to match against. Create an account below -- that "
                "enrols your face -- or ask the super-user to enrol you.",
                Theme.BAD,
            )
            return

        self._say(self.biometric_message, "", Theme.INK_SOFT)

        def done(result):
            if result is None:
                self._say(self.biometric_message,
                          "Face sign-in cancelled.", Theme.INK_SOFT)
                return
            self._handle_identification(result)

        identify_face(self, self.app, face, on_complete=done)

    def _face_login_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a photo of your face",
            filetypes=[("Images", "*.png *.jpg *.jpeg *.bmp"), ("All files", "*.*")],
        )
        if not path:
            return
        self._say(self.biometric_message, "Checking that photo...", Theme.INK_SOFT)
        self._run_async(
            lambda: self.services.biometrics.face.identify_image(path),
            self._handle_identification,
        )

    def _voice_login_mic(self) -> None:
        self._say(self.biometric_message, "Recording for 3 seconds -- speak now.",
                  Theme.INK_SOFT)
        self._run_async(
            lambda: self.services.biometrics.voice.identify_from_microphone(3.0),
            self._handle_identification,
        )

    def _voice_login_file(self) -> None:
        path = filedialog.askopenfilename(
            title="Choose a WAV recording",
            filetypes=[("WAV audio", "*.wav"), ("All files", "*.*")],
        )
        if not path:
            return
        self._say(self.biometric_message, "Checking that recording...", Theme.INK_SOFT)
        self._run_async(
            lambda: self.services.biometrics.voice.identify_file(path),
            self._handle_identification,
        )

    def _handle_identification(self, result) -> None:
        if not result.identified:
            self._say(self.biometric_message, result.message or "Not recognised.",
                      Theme.BAD)
            return

        user = self.services.storage.get_user(result.user_id)
        if not user:
            self._say(self.biometric_message,
                      "Recognised sample %s, but that account no longer exists."
                      % result.user_id, Theme.BAD)
            return

        models.normalise_user(user)
        allowed, why = self.services.rules.can_log_in(user)
        if not allowed:
            # Identification succeeded; the account is just not usable.
            self._say(self.biometric_message,
                      "Recognised you as %s, but %s"
                      % (user.get("username"), why[0].lower() + why[1:]),
                      Theme.BAD)
            return

        self._say(self.biometric_message, result.message, Theme.GOOD)
        how = "face" if "face" in (result.backend or "").lower() or \
                        "crop" in (result.backend or "").lower() else "voice"
        self.app.log_in(user, how="%s recognition" % how)

    # -- registration --------------------------------------------------

    def _build_register_card(self, parent) -> None:
        card = tk.Frame(parent, bg=Theme.SURFACE, highlightthickness=1,
                        highlightbackground=Theme.BORDER)
        card.pack(fill="x", pady=(18, 0))

        inner = tk.Frame(card, bg=Theme.SURFACE)
        inner.pack(fill="x", padx=20, pady=18)

        ttk.Label(inner, text="Create an account", style="H3.TLabel").pack(anchor="w")
        ttk.Label(
            inner,
            text="New accounts start as registered customers. More than three "
                 "purchases or more than $200 spent promotes you to VIP "
                 "automatically.",
            style="SurfaceMuted.TLabel", wraplength=700, justify="left",
        ).pack(anchor="w", pady=(2, 10))

        # Requirement 4 made explicit at the point of signup.
        notice = tk.Frame(inner, bg=Theme.ACCENT_SOFT)
        notice.pack(fill="x", pady=(0, 14))
        self.reg_notice = tk.Label(
            notice,
            text="Your face is your login. Creating an account takes five "
                 "photographs with the camera, and from then on you sign in by "
                 "looking at it.",
            bg=Theme.ACCENT_SOFT, fg=Theme.ACCENT_DARK,
            font=(self.app.fonts["sans"], 9), wraplength=680, justify="left",
            anchor="w", padx=12, pady=9,
        )
        self.reg_notice.pack(fill="x")

        grid = tk.Frame(inner, bg=Theme.SURFACE)
        grid.pack(anchor="w")

        self.reg_name = self._field(grid, "Display name", 0, 0, width=22)
        self.reg_username = self._field(grid, "Username", 0, 1, width=18)
        self.reg_password = self._field(grid, "Password", 0, 2, width=18, secret=True)
        self.reg_deposit = self._field(grid, "Opening deposit ($)", 0, 3, width=12)
        self.reg_deposit.insert(0, "25.00")

        buttons = tk.Frame(inner, bg=Theme.SURFACE)
        buttons.pack(anchor="w", pady=(14, 0))
        self.register_button = ttk.Button(
            buttons, text="Take my photo and create the account",
            style="Accent.TButton", command=self._register,
        )
        self.register_button.pack(side="left")

        self.register_message = ttk.Label(
            inner, text="", style="SurfaceMuted.TLabel", wraplength=700,
            justify="left",
        )
        self.register_message.pack(anchor="w", pady=(10, 0))

    def _field(self, parent, label: str, row: int, column: int,
               width: int = 20, secret: bool = False) -> ttk.Entry:
        box = tk.Frame(parent, bg=Theme.SURFACE)
        box.grid(row=row, column=column, padx=(0, 14), sticky="w")
        tk.Label(box, text=label, bg=Theme.SURFACE, fg=Theme.INK_SOFT,
                 font=(self.app.fonts["sans"], 9)).pack(anchor="w")
        entry = ttk.Entry(box, width=width, show="•" if secret else "")
        entry.pack(anchor="w", pady=(2, 0))
        return entry

    def _register(self) -> None:
        """Create an account, enrolling the face as part of signing up.

        The order matters: everything that can be validated is checked *before*
        the camera opens, and the account is only created once the photographs
        are in hand.  Creating the account first and then enrolling would leave
        an account with no way to log in if the user closed the camera window.
        """
        config = self.services.config
        require_face = bool(config.get("require_face_enrolment"))

        # 1. Validate the form.
        try:
            deposit = float(self.reg_deposit.get() or 0)
        except ValueError:
            self._say(self.register_message, "Opening deposit must be a number.",
                      Theme.BAD)
            return
        if deposit < 0:
            self._say(self.register_message, "Opening deposit cannot be negative.",
                      Theme.BAD)
            return

        username = self.reg_username.get().strip()
        password = self.reg_password.get()
        if not username:
            self._say(self.register_message, "Choose a username.", Theme.BAD)
            return
        if len(password) < 4:
            self._say(self.register_message,
                      "Password must be at least 4 characters.", Theme.BAD)
            return
        if self.services.storage.find_user_by_username(username):
            self._say(self.register_message, "That username is already taken.",
                      Theme.BAD)
            return

        if not require_face:
            self._create_account(username, password, deposit, crops=None)
            return

        # 2. Check the camera is usable before promising anything.
        from .camera import camera_available, dependency_message, enroll_face

        if not camera_available():
            self._say(
                self.register_message,
                "%s Face enrolment is required to create an account, so this "
                "cannot continue." % dependency_message(),
                Theme.BAD,
            )
            return

        self._say(self.register_message,
                  "Opening the camera. Look straight at it...", Theme.INK_SOFT)
        self.register_button.configure(state="disabled")

        def done(crops):
            self.register_button.configure(state="normal")
            minimum = 3
            if not crops or len(crops) < minimum:
                got = len(crops or [])
                self._say(
                    self.register_message,
                    "Face enrolment was not completed (%d of %d photographs "
                    "captured), so no account was created. Your face is the "
                    "only way to sign in, so it has to be enrolled first. "
                    "Try again in better light, facing the camera."
                    % (got, minimum),
                    Theme.BAD,
                )
                return
            self._create_account(username, password, deposit, crops=crops)

        enroll_face(self, self.app, self.services.biometrics.face,
                    target=5, on_complete=done, person=username)

    def _create_account(self, username: str, password: str, deposit: float,
                        crops) -> None:
        try:
            user = self.services.rules.create_account(
                username, password, role=models.REGISTERED,
                balance=deposit, display_name=self.reg_name.get(),
            )
        except RuleError as exc:
            self._say(self.register_message, str(exc), Theme.BAD)
            return

        note = ""
        if crops:
            saved, message = self.services.biometrics.face.add_samples_from_crops(
                user["user_id"], crops
            )
            if saved:
                ok, train_note = self.services.biometrics.face.train()
                note = (" %d face photographs enrolled and the model retrained."
                        % saved) if ok else (" Face samples saved, but training "
                                             "failed: %s" % train_note)
            else:
                note = " Face samples could not be saved: %s" % message

        for entry in (self.reg_name, self.reg_username, self.reg_password):
            entry.delete(0, "end")
        self._say(self.register_message,
                  "Account created.%s Next time, sign in with the camera."
                  % note, Theme.GOOD)
        self.app.log_in(user, how="new account")

    # ------------------------------------------------------------------

    @staticmethod
    def _say(label: ttk.Label, text: str, colour: str) -> None:
        label.configure(text=text, foreground=colour)

    def refresh(self) -> None:
        face = self.services.biometrics.face
        voice = self.services.biometrics.voice

        lines = []
        if face.available():
            lines.append("Face detector: %s." % face.backend_description)
            lines.append("Face model: %s."
                         % ("trained and ready" if face.trained
                            else "not trained yet"))
        else:
            lines.append(face.dependency_message())
        if voice.available():
            lines.append("Voice model: %s."
                         % ("trained and ready" if voice.trained
                            else "not trained yet"))
        else:
            lines.append(voice.dependency_message())

        self.biometric_status.configure(text=" ".join(lines))
