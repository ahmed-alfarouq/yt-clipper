import customtkinter as ctk


class TimeInput(ctk.CTkFrame):
    """Segmented HH:MM:SS input with up/down steppers, scroll wheel, and arrow keys."""

    def __init__(self, master, on_change=None, **kwargs):
        super().__init__(master, fg_color="transparent", **kwargs)
        self.on_change = on_change

        self.h_var = ctk.StringVar(value="00")
        self.m_var = ctk.StringVar(value="00")
        self.s_var = ctk.StringVar(value="00")

        self._build_segment(self.h_var, "H", 0, uncapped=True)
        ctk.CTkLabel(self, text=":", font=ctk.CTkFont(size=18, weight="bold")).grid(row=0, column=1, padx=2)
        self._build_segment(self.m_var, "M", 2)
        ctk.CTkLabel(self, text=":", font=ctk.CTkFont(size=18, weight="bold")).grid(row=0, column=3, padx=2)
        self._build_segment(self.s_var, "S", 4)

    def _build_segment(self, var, label, col, uncapped=False):
        frame = ctk.CTkFrame(self, fg_color="transparent")
        frame.grid(row=0, column=col, padx=3)

        ctk.CTkButton(frame, text="▲", width=34, height=16,
                      command=lambda: self._step(var, 1, uncapped)).pack()

        entry = ctk.CTkEntry(frame, textvariable=var, width=48, height=38,
                              justify="center", font=ctk.CTkFont(size=15))
        entry.pack(pady=2)
        entry.bind("<FocusOut>", lambda e: self._normalize(var, uncapped))
        entry.bind("<Return>", lambda e: self._normalize(var, uncapped))
        entry.bind("<Up>", lambda e: self._step(var, 1, uncapped))
        entry.bind("<Down>", lambda e: self._step(var, -1, uncapped))
        entry.bind("<MouseWheel>", lambda e: self._step(var, 1 if e.delta > 0 else -1, uncapped))

        ctk.CTkButton(frame, text="▼", width=34, height=16,
                      command=lambda: self._step(var, -1, uncapped)).pack()

        ctk.CTkLabel(frame, text=label, font=ctk.CTkFont(size=10), text_color="gray").pack(pady=(2, 0))

    def _step(self, var, delta, uncapped):
        try:
            val = int(var.get())
        except ValueError:
            val = 0
        val = max(0, val + delta)
        if not uncapped:
            val = val % 60
        var.set(f"{val:02d}")
        self._emit_change()

    def _normalize(self, var, uncapped):
        try:
            val = int(var.get())
        except ValueError:
            val = 0
        val = max(0, val) if uncapped else max(0, min(val, 59))
        var.set(f"{val:02d}")
        self._emit_change()

    def _emit_change(self):
        if self.on_change:
            self.on_change(self.get_seconds())

    def get_seconds(self):
        try:
            h, m, s = int(self.h_var.get() or 0), int(self.m_var.get() or 0), int(self.s_var.get() or 0)
            return h * 3600 + m * 60 + s
        except ValueError:
            return 0

    def set_seconds(self, total_seconds):
        total_seconds = max(0, int(total_seconds))
        h, rem = divmod(total_seconds, 3600)
        m, s = divmod(rem, 60)
        self.h_var.set(f"{h:02d}")
        self.m_var.set(f"{m:02d}")
        self.s_var.set(f"{s:02d}")