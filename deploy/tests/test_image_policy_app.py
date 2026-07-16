import unittest
from unittest.mock import patch

from fastapi import HTTPException

import image_policy_app


class ImagePolicyTests(unittest.TestCase):
    def request(self, size_bytes: int) -> image_policy_app.ImagePolicyRequest:
        return image_policy_app.ImagePolicyRequest(
            service="example-portal",
            image="registry.example.org/example/portal:revision",
            size_bytes=size_bytes,
        )

    def test_accepts_image_at_limit(self):
        with (
            patch.object(image_policy_app, "MAX_IMAGE_SIZE_BYTES", 456),
            patch.object(image_policy_app, "authorize_service") as authorize,
        ):
            result = image_policy_app.check_image_policy(self.request(456), "Bearer test")

        authorize.assert_called_once_with("Bearer test", "example-portal")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["max_image_size_bytes"], 456)

    def test_rejects_image_over_limit(self):
        with (
            patch.object(image_policy_app, "MAX_IMAGE_SIZE_BYTES", 456),
            patch.object(image_policy_app, "authorize_service"),
            self.assertRaises(HTTPException) as failure,
        ):
            image_policy_app.check_image_policy(self.request(457), "Bearer test")

        self.assertEqual(failure.exception.status_code, 413)
        self.assertEqual(failure.exception.detail["code"], "image_too_large")
        self.assertEqual(failure.exception.detail["size_bytes"], 457)
        self.assertEqual(failure.exception.detail["max_image_size_bytes"], 456)

    def test_feature_is_advertised(self):
        self.assertIn("image-size-policy", image_policy_app.ARCTURUS_FEATURES)


if __name__ == "__main__":
    unittest.main()
