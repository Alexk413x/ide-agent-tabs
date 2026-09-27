package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.net.InetAddress

class AdmissionTest {

    private val token = newToken()
    private val json = mapOf("Content-Type" to "application/json", "Authorization" to "Bearer $token")

    private fun check(address: String?, method: String = "POST", headers: Map<String, String> = json) =
        Admission.check(address?.let { InetAddress.getByName(it) }, method, token) { headers[it] }

    @Test
    fun `loopback POST with JSON is admitted`() {
        assertNull(check("127.0.0.1"))
        assertNull(check("::1"))
        assertNull(check("127.0.0.1", headers = json + ("Content-Type" to "application/json; charset=utf-8")))
    }

    @Test
    fun `non-loopback address is refused`() {
        for (address in listOf("192.168.1.20", "10.0.0.5", "0.0.0.0", "8.8.8.8", "fe80::1")) {
            assertEquals(address, 403, check(address)?.status)
        }
    }

    @Test
    fun `missing address is refused`() {
        assertEquals(403, check(null)?.status)
    }

    @Test
    fun `other methods are refused`() {
        assertEquals(405, check("127.0.0.1", method = "GET")?.status)
        assertEquals(405, check("127.0.0.1", method = "OPTIONS")?.status)
    }

    @Test
    fun `browser requests are refused`() {
        assertEquals(403, check("127.0.0.1", headers = json + ("Origin" to "https://example.com"))?.status)
        assertEquals(403, check("127.0.0.1", headers = json + ("Origin" to "null"))?.status)
        assertEquals(403, check("127.0.0.1", headers = json + ("Referer" to "https://example.com/"))?.status)
    }

    @Test
    fun `non-JSON content type is refused`() {
        val auth = json - "Content-Type"
        assertEquals(415, check("127.0.0.1", headers = auth)?.status)
        assertEquals(415, check("127.0.0.1", headers = auth + ("Content-Type" to "text/plain"))?.status)
        assertEquals(415, check("127.0.0.1", headers = auth + ("Content-Type" to "application/x-www-form-urlencoded"))?.status)
    }

    @Test
    fun `missing or wrong token is refused`() {
        val noAuth = json - "Authorization"
        assertEquals(401, check("127.0.0.1", headers = noAuth)?.status)
        for (bad in listOf("", "Bearer", "Bearer ", "Basic $token", token, "Bearer ${token.dropLast(1)}", "Bearer ${token}0", "Bearer ${newToken()}")) {
            assertEquals(bad, 401, check("127.0.0.1", headers = noAuth + ("Authorization" to bad))?.status)
        }
    }

    @Test
    fun `token check ignores the scheme's case and surrounding spaces`() {
        assertTrue(Admission.bearerMatches("bearer $token", token))
        assertTrue(Admission.bearerMatches("  Bearer   $token  ", token))
        assertFalse(Admission.bearerMatches("Bearer ${token.uppercase()}", token))
        assertFalse(Admission.bearerMatches("Bearer ", ""))
        assertFalse(Admission.bearerMatches(null, token))
    }

    @Test
    fun `token is checked after the browser rules and before the content type`() {
        assertEquals(403, check("127.0.0.1", headers = mapOf("Origin" to "https://example.com"))?.status)
        assertEquals(401, check("127.0.0.1", headers = emptyMap())?.status)
    }

    @Test
    fun `tokens are 64 hex characters and fresh each time`() {
        assertTrue(token.matches(Regex("[0-9a-f]{64}")))
        assertNotEquals(token, newToken())
    }
}
