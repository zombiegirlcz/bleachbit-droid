package cz.zombie.bleachbitdroid

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Drives [ShellSession] against a real `sh`.
 *
 * The session protocol (marker line plus `$?`) is the one piece of this app
 * that is pure JVM and can therefore be tested without a device, and it is
 * also the piece every shell-backed cleaner depends on. These tests exist
 * because the protocol was previously only ever verified by hand.
 */
class ShellSessionTest {

    private fun session() = ShellSession("ShellSessionTest") {
        ProcessBuilder("sh").start()
    }

    @Test
    fun `reports stdout and a zero exit code`() {
        val s = session()
        try {
            val r = s.exec("echo hello")
            assertEquals(0, r.code)
            assertEquals("hello", r.stdout.trim())
        } finally {
            s.close()
        }
    }

    @Test
    fun `reports the exit code of a failing command`() {
        val s = session()
        try {
            // A subshell on purpose: a bare `exit` inside the protocol's brace
            // group would exit the session's own shell. Verified against a real
            // `sh` - no backend ever sends `exit`, but a test must not either.
            assertEquals(3, s.exec("(exit 3)").code)
            assertEquals(1, s.exec("test -e /definitely/not/here").code)
            // The shell survives a failure and keeps serving commands.
            assertEquals(0, s.exec("echo still here").code)
        } finally {
            s.close()
        }
    }

    @Test
    fun `folds stderr into stdout in order`() {
        val s = session()
        try {
            val r = s.exec("echo out; echo err 1>&2")
            assertEquals(0, r.code)
            assertTrue(r.stdout, r.stdout.contains("out"))
            assertTrue(r.stdout, r.stdout.contains("err"))
        } finally {
            s.close()
        }
    }

    @Test
    fun `keeps tabs intact`() {
        // `list_sized()` parses `stat -c '%s\t%n'` output by splitting on TAB,
        // so the session must not mangle it.
        val s = session()
        try {
            val r = s.exec("printf '12\\tsome/path\\n'")
            assertTrue(r.stdout, r.stdout.contains("12\tsome/path"))
        } finally {
            s.close()
        }
    }

    @Test
    fun `does not mix up the output of consecutive commands`() {
        val s = session()
        try {
            assertEquals("first", s.exec("echo first").stdout.trim())
            assertEquals("second", s.exec("echo second").stdout.trim())
            assertEquals("third", s.exec("echo third").stdout.trim())
        } finally {
            s.close()
        }
    }

    @Test
    fun `a closed session serves the next command`() {
        // Regression: `close()` used to latch a `closed` flag that was never
        // cleared, so one timeout, one dead Shizuku binder or one refused root
        // prompt made every later command return "session closed" until the
        // app was restarted - the whole privilege tier stayed dead.
        val s = session()
        try {
            assertEquals(0, s.exec("echo before").code)
            s.close()
            val r = s.exec("echo after")
            assertEquals(0, r.code)
            assertEquals("after", r.stdout.trim())
        } finally {
            s.close()
        }
    }

    @Test
    fun `recovers from a timeout`() {
        val s = session()
        try {
            val timedOut = s.exec("sleep 5", 1)
            assertEquals(-1, timedOut.code)
            assertTrue(timedOut.stderr, timedOut.stderr.contains("timeout"))

            val after = s.exec("echo recovered")
            assertEquals(0, after.code)
            assertEquals("recovered", after.stdout.trim())
        } finally {
            s.close()
        }
    }
}
