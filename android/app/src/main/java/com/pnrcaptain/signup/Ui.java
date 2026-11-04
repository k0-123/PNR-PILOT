package com.pnrcaptain.signup;

import android.app.Activity;
import android.graphics.Color;
import android.graphics.Typeface;
import android.text.InputType;
import android.util.TypedValue;
import android.view.ViewGroup;
import android.widget.Button;
import android.widget.EditText;
import android.widget.LinearLayout;
import android.widget.TextView;

/** Tiny helpers so the screens can be built in code (no XML layouts). */
final class Ui {
    private Ui() {}

    static int dp(Activity a, int v) {
        return (int) TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, v, a.getResources().getDisplayMetrics());
    }

    static LinearLayout column(Activity a) {
        LinearLayout l = new LinearLayout(a);
        l.setOrientation(LinearLayout.VERTICAL);
        int p = dp(a, 16);
        l.setPadding(p, p, p, p);
        return l;
    }

    static LinearLayout row(Activity a) {
        LinearLayout l = new LinearLayout(a);
        l.setOrientation(LinearLayout.HORIZONTAL);
        return l;
    }

    static TextView title(Activity a, String text) {
        TextView t = text(a, text);
        t.setTextSize(20);
        t.setTypeface(Typeface.DEFAULT_BOLD);
        t.setPadding(0, 0, 0, dp(a, 8));
        return t;
    }

    static TextView text(Activity a, String text) {
        TextView t = new TextView(a);
        t.setText(text);
        t.setTextSize(15);
        t.setTextColor(Color.parseColor("#222222"));
        t.setPadding(0, dp(a, 4), 0, dp(a, 4));
        return t;
    }

    static TextView small(Activity a, String text) {
        TextView t = text(a, text);
        t.setTextSize(13);
        t.setTextColor(Color.parseColor("#666666"));
        return t;
    }

    static EditText input(Activity a, String hint, String value, boolean secret) {
        EditText e = new EditText(a);
        e.setHint(hint);
        e.setText(value);
        e.setSingleLine(true);
        e.setInputType(secret ? InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_PASSWORD
                : InputType.TYPE_CLASS_TEXT | InputType.TYPE_TEXT_VARIATION_URI);
        return e;
    }

    static Button button(Activity a, String text, Runnable onClick) {
        Button b = new Button(a);
        b.setText(text);
        b.setAllCaps(false);
        b.setOnClickListener(v -> onClick.run());
        return b;
    }

    /** Equal-width weight for buttons inside a row(). */
    static LinearLayout.LayoutParams weight() {
        return new LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f);
    }
}
