package dev.jcmonteiro.mangakindle;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.SharedPreferences;
import android.os.Bundle;
import android.text.InputType;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;

/** Thin shell around the manga-kindle web app that runs on the Mac. */
public class MainActivity extends Activity {
    private static final String[] DEFAULTS = {
        "http://Julios-Mac-mini.local:8766",
        "http://192.168.15.4:8766",
    };

    private SharedPreferences prefs;
    private WebView web;
    private int attempt = 0;
    private boolean dialogOpen = false;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        prefs = getSharedPreferences("app", MODE_PRIVATE);

        web = new WebView(this);
        setContentView(web);
        WebSettings s = web.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);

        web.setWebViewClient(new WebViewClient() {
            @Override
            public void onReceivedError(WebView view, WebResourceRequest req, WebResourceError err) {
                if (req.isForMainFrame()) onLoadFailed();
            }

            @Override
            public void onPageFinished(WebView view, String url) {
                prefs.edit().putString("working", url).apply();
            }
        });

        web.loadUrl(prefs.getString("server", DEFAULTS[0]));
    }

    private void onLoadFailed() {
        if (dialogOpen) return;
        // Try the built-in fallbacks (hostname, then IP) before asking the user.
        if (attempt < DEFAULTS.length && prefs.getString("server", null) == null) {
            web.loadUrl(DEFAULTS[attempt++]);
            return;
        }
        dialogOpen = true;
        EditText input = new EditText(this);
        input.setInputType(InputType.TYPE_TEXT_VARIATION_URI);
        input.setText(prefs.getString("server", DEFAULTS[0]));
        new AlertDialog.Builder(this)
            .setTitle("Can't reach your Mac")
            .setMessage("Check that you are on the same Wi-Fi, or set the server address.")
            .setView(input)
            .setCancelable(false)
            .setPositiveButton("Save & retry", (d, w) -> {
                dialogOpen = false;
                String url = input.getText().toString().trim();
                if (!url.startsWith("http")) url = "http://" + url;
                prefs.edit().putString("server", url).apply();
                web.loadUrl(url);
            })
            .setNegativeButton("Close", (d, w) -> finish())
            .show();
    }

    @Override
    public void onBackPressed() {
        if (web.canGoBack()) web.goBack();
        else super.onBackPressed();
    }
}
