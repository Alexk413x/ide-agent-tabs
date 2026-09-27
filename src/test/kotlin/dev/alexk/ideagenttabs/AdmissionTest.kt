package dev.alexk.ideagenttabs

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Test
import java.net.InetAddress

class AdmissionTest {

    private val json = mapOf("Content-Type" to "application/json")

    private fun check(address: String?, method: String = "POST", headers: Map<String, String> = json) =
        Admission.check(address?.let { InetAddress.getByName(it) }, method) { headers[it] }

    @Test
    fun `loopback POST with JSON is admitted`() {
        assertNull(check("127.0.0.1"))
        assertNull(check("::1"))
        assertNull(check("127.0.0.1", headers = mapOf("Content-Type" to "application/json; charset=utf-8")))
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
        assertEquals(415, check("127.0.0.1", headers = emptyMap())?.status)
        assertEquals(415, check("127.0.0.1", headers = mapOf("Content-Type" to "text/plain"))?.status)
        assertEquals(415, check("127.0.0.1", headers = mapOf("Content-Type" to "application/x-www-form-urlencoded"))?.status)
    }
}
