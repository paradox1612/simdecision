package dev.simdecision;

import android.accessibilityservice.AccessibilityServiceInfo;
import android.app.UiAutomation;
import android.graphics.Rect;
import android.net.LocalServerSocket;
import android.net.LocalSocket;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;

import java.io.BufferedReader;
import java.io.InputStreamReader;
import java.io.OutputStream;
import java.nio.charset.StandardCharsets;
import java.util.List;

/**
 * Persistent screen-snapshot helper, run on the emulator with app_process (shell uid, no APK, no install):
 *
 *   CLASSPATH=/data/local/tmp/simdecision-snap.jar:/system/framework/uiautomator.jar \
 *     app_process /system/bin dev.simdecision.Snap [socket-name] [idle-exit-seconds]
 *
 * `uiautomator dump` pays ~1 s of JVM start plus an accessibility connect and an idle wait on EVERY call. This process
 * connects once and answers "dump" over a local socket with the same XML format (all windows, bottom-most first, so a
 * node later in the document is drawn on top). Only one UiAutomation client can exist per device, so the helper exits
 * after `idle-exit-seconds` without a request, freeing the connection for other tools.
 */
public final class Snap {
    private static UiAutomation ua;
    private static volatile long last = System.currentTimeMillis();

    public static void main(String[] args) throws Exception {
        String name = args.length > 0 ? args[0] : "simdecision-snap";
        final long idleMs = (args.length > 1 ? Long.parseLong(args[1]) : 120) * 1000L;
        Class<?> c = Class.forName("com.android.uiautomator.core.UiAutomationShellWrapper");
        Object w = c.getConstructor().newInstance();
        c.getMethod("connect").invoke(w);
        ua = (UiAutomation) c.getMethod("getUiAutomation").invoke(w);
        AccessibilityServiceInfo info = ua.getServiceInfo();
        info.flags |= AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                | AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS
                | AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS;
        ua.setServiceInfo(info);

        Thread watchdog = new Thread(() -> {
            while (true) {
                try { Thread.sleep(1000); } catch (InterruptedException e) { return; }
                if (System.currentTimeMillis() - last > idleMs) System.exit(0);
            }
        });
        watchdog.setDaemon(true);
        watchdog.start();

        LocalServerSocket server = new LocalServerSocket(name);
        System.out.println("READY " + name);
        System.out.flush();
        while (true) {
            try (LocalSocket s = server.accept()) {
                last = System.currentTimeMillis();
                BufferedReader in = new BufferedReader(new InputStreamReader(s.getInputStream(), StandardCharsets.UTF_8));
                String cmd = in.readLine();
                OutputStream out = s.getOutputStream();
                if ("quit".equals(cmd)) {
                    out.write("bye\n".getBytes(StandardCharsets.UTF_8));
                    out.flush();
                    System.exit(0);
                } else if ("ping".equals(cmd)) {
                    out.write("pong\n".getBytes(StandardCharsets.UTF_8));
                } else {
                    if ("dump_idle".equals(cmd)) {
                        try { ua.waitForIdle(100, 2000); } catch (Exception ignored) { }
                    }
                    out.write(dump().getBytes(StandardCharsets.UTF_8));
                }
                out.flush();
            } catch (Exception e) {
                e.printStackTrace();
            }
        }
    }

    /** Drop UiAutomation's node cache so every dump is a fresh read. The cache is only as good as the accessibility
     *  events the app sends, and a Compose app moving from splash to its first screen can leave it stale for good. */
    private static void clearCache() {
        try {
            UiAutomation.class.getMethod("clearCache").invoke(ua);  // API 34+
        } catch (Exception e) {
            // older Android: no cache API; roots are re-fetched per window below anyway
        }
    }

    private static String dump() {
        clearCache();
        StringBuilder sb = new StringBuilder(64 * 1024);
        sb.append("<?xml version='1.0' encoding='UTF-8' standalone='yes' ?><hierarchy rotation=\"0\">");
        List<AccessibilityWindowInfo> windows = ua.getWindows();
        Rect all = new Rect();
        if (windows != null) {
            Rect r = new Rect();
            for (AccessibilityWindowInfo win : windows) { win.getBoundsInScreen(r); all.union(r); }
        }
        // A synthetic screen-sized root keeps the old contract: the first <node> carries the display size.
        sb.append("<node index=\"0\" text=\"\" resource-id=\"\" class=\"simdecision.Screen\" package=\"\" content-desc=\"\" ")
          .append("checkable=\"false\" checked=\"false\" clickable=\"false\" enabled=\"true\" focusable=\"false\" focused=\"false\" ")
          .append("scrollable=\"false\" long-clickable=\"false\" password=\"false\" selected=\"false\" bounds=\"")
          .append(bounds(all)).append("\">");
        if (windows == null || windows.isEmpty()) {
            AccessibilityNodeInfo root = ua.getRootInActiveWindow();
            if (root != null) node(sb, root, 0);
        } else {
            // getWindows() is top-most first; emit bottom-most first so document order == drawing order.
            for (int i = windows.size() - 1; i >= 0; i--) {
                AccessibilityNodeInfo root = windows.get(i).getRoot();
                if (root != null) node(sb, root, windows.size() - 1 - i);
            }
        }
        sb.append("</node></hierarchy>");
        return sb.toString();
    }

    private static void node(StringBuilder sb, AccessibilityNodeInfo n, int index) {
        if (!n.isVisibleToUser()) return;
        Rect r = new Rect();
        n.getBoundsInScreen(r);
        sb.append("<node index=\"").append(index)
          .append("\" text=\"").append(esc(n.getText()))
          .append("\" resource-id=\"").append(esc(n.getViewIdResourceName()))
          .append("\" class=\"").append(esc(n.getClassName()))
          .append("\" package=\"").append(esc(n.getPackageName()))
          .append("\" content-desc=\"").append(esc(n.getContentDescription()))
          .append("\" checkable=\"").append(n.isCheckable())
          .append("\" checked=\"").append(n.isChecked())
          .append("\" clickable=\"").append(n.isClickable())
          .append("\" enabled=\"").append(n.isEnabled())
          .append("\" focusable=\"").append(n.isFocusable())
          .append("\" focused=\"").append(n.isFocused())
          .append("\" scrollable=\"").append(n.isScrollable())
          .append("\" long-clickable=\"").append(n.isLongClickable())
          .append("\" password=\"").append(n.isPassword())
          .append("\" selected=\"").append(n.isSelected())
          .append("\" bounds=\"").append(bounds(r)).append("\">");
        for (int i = 0; i < n.getChildCount(); i++) {
            AccessibilityNodeInfo child = n.getChild(i);
            if (child != null) node(sb, child, i);
        }
        sb.append("</node>");
    }

    private static String bounds(Rect r) {
        return "[" + r.left + "," + r.top + "][" + r.right + "," + r.bottom + "]";
    }

    private static String esc(CharSequence cs) {
        if (cs == null) return "";
        StringBuilder b = new StringBuilder(cs.length());
        for (int i = 0; i < cs.length(); i++) {
            char ch = cs.charAt(i);
            switch (ch) {
                case '&': b.append("&amp;"); break;
                case '<': b.append("&lt;"); break;
                case '>': b.append("&gt;"); break;
                case '"': b.append("&quot;"); break;
                case '\n': b.append("&#10;"); break;
                default:
                    if (ch < 0x20 && ch != '\t') continue;  // XML 1.0 forbids other control characters
                    b.append(ch);
            }
        }
        return b.toString();
    }
}
