# -*- coding: utf-8 -*-
import unittest

import gateway


class ImgPathFromUrlTests(unittest.TestCase):
    def test_query_still_works(self):
        self.assertEqual(
            r"D:\图\a.png",
            gateway.img_path_from_url("/img?p=D%3A%5C%E5%9B%BE%5Ca.png"))

    def test_path_style_survives_old_webgw_dropping_query(self):
        self.assertEqual("D:/图/a.png", gateway.img_path_from_url("/img/D:/图/a.png"))
        self.assertEqual(
            "D:/图/a.png",
            gateway.img_path_from_url("/img/D%3A/%E5%9B%BE/a.png"))

    def test_bare_img_is_empty(self):
        self.assertEqual("", gateway.img_path_from_url("/img"))


class ShimTimeoutTests(unittest.TestCase):
    def test_install_hooks_get_a_minute(self):
        self.assertIn("park_install_hook: 60000", gateway._SHIM)
        self.assertIn("bill_install_hook: 60000", gateway._SHIM)
        self.assertIn("park_restore: 60000", gateway._SHIM)
        self.assertIn("bill_uninstall: 60000", gateway._SHIM)
        self.assertIn("list_native_models: 40000", gateway._SHIM)
        self.assertIn("create_native_task: 120000", gateway._SHIM)

    def test_http_error_is_thrown_not_swallowed(self):
        self.assertIn("if (!r.ok) throw new Error", gateway._SHIM)


if __name__ == "__main__":
    unittest.main()
