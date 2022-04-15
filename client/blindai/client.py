# Copyright 2022 Mithril Security. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import logging
import os
import ssl
from enum import IntEnum
import socket
from typing import Any, List, Optional, Tuple
from dataclasses import dataclass
from hashlib import sha256

from cbor2 import dumps
from dcap_attestation import (
    get_server_cert,
    load_policy,
    verify_claims,
    verify_dcap_attestation,
)
from grpc import RpcError, secure_channel, ssl_channel_credentials

# These modules are generated by grpc proto compiler, from proto files in proto
from securedexchange_pb2 import (
    SendModelRequest,
    RunModelRequest,
    DeleteModelRequest,
    Payload,
    DatumTypeEnum,
)
from securedexchange_pb2_grpc import ExchangeStub
from untrusted_pb2 import (
    GetCertificateRequest as certificate_request,
    GetSgxQuoteWithCollateralRequest as quote_request,
    GetServerInfoRequest as server_info_request,
)
from untrusted_pb2_grpc import AttestationStub
from cryptography.exceptions import InvalidSignature

from utils.utils import (
    create_byte_chunk,
    encode_certificate,
    strip_https,
    get_enclave_signing_key,
    supported_server_version,
    ProofData,
)
from utils.errors import (
    check_rpc_exception,
    check_socket_exception,
    SignatureError,
    VersionError,
)

CONNECTION_TIMEOUT = 10

ModelDatumType = IntEnum("ModelDatumType", DatumTypeEnum.items())


@dataclass
class UploadModelResponse:
    proof: Optional[ProofData]
    model_id: str


@dataclass
class RunModelResponse:
    proof: Optional[ProofData]
    output: List[float]


@dataclass
class DeleteModelResponse:
    proof: Optional[ProofData]


class BlindAiClient:
    def __init__(self, debug_mode=False):

        self.channel = None
        self.policy = None
        self.stub = None
        self.enclave_signing_key = None

        if debug_mode:
            os.environ["GRPC_TRACE"] = "transport_security,tsi"
            os.environ["GRPC_VERBOSITY"] = "DEBUG"
        self.SIMULATION_MODE = False

    def _is_connected(self):
        return self.channel is not None

    def _close_channel(self):
        if self._is_connected():
            self.channel.close()

    def connect_server(
        self,
        addr: str,
        server_name: str = "blindai-srv",
        policy: str = "",
        certificate: str = "",
        simulation: bool = False,
        untrusted_port: int = 50052,
        attested_port: int = 50051,
    ):
        """Connect to the server with the specified parameters.
        You will have to specify here the expected policy (server identity, configuration...)
        and the server TLS certificate, if you are using the hardware mode.

        If you're using the simulation mode, you don't need to provide a policy and certificate,
        but please keep in mind that this mode should NEVER be used in production as it doesn't
        have most of the security provided by the hardware mode.

        Args:
            addr: The address of BlindAI server you want to reach.
            server_name: Contains the CN expected by the server TLS certificate.
            policy: Path to the toml file describing the policy of the server.
                Generated in the server side.
            certificate: Path to the public key of the untrusted inference server.
                Generated in the server side.
            simulation:  Connect to the server in simulation mode (default False).
                If set to True, the args policy and certificate will be ignored.
            untrusted_port: untrusted connection server port, default 50052
            attested_port: attested connection server port, dedault 50051
        Raises:
            VersionError: Will be raised if the version of the server is not supported by the client.
            ValueError: Will be raised in case the policy doesn't match the
                server identity and configuration.
            ConnectionError: will be raised if the connection with the server fails.
            FileNotFoundError: will be raised if the policy file, or the certificate file is not
                found (in Hardware mode).
        """
        self.SIMULATION_MODE = simulation
        self.DISABLE_UNTRUSTED_SERVER_CERT_CHECK = simulation
        error = None

        addr = strip_https(addr)

        untrusted_client_to_enclave = addr + ":" + str(untrusted_port)
        attested_client_to_enclave = addr + ":" + str(attested_port)

        if self.DISABLE_UNTRUSTED_SERVER_CERT_CHECK:
            logging.warning("Untrusted server certificate check bypassed")

            try:
                socket.setdefaulttimeout(CONNECTION_TIMEOUT)
                untrusted_server_cert = ssl.get_server_certificate(
                    (addr, untrusted_port)
                )
                untrusted_server_creds = ssl_channel_credentials(
                    root_certificates=bytes(untrusted_server_cert, encoding="utf8")
                )

            except RpcError as rpc_error:
                error = ConnectionError(check_rpc_exception(rpc_error))

            except socket.error as socket_error:
                error = ConnectionError(check_socket_exception(socket_error))

            finally:
                if error is not None:
                    raise error
        else:
            with open(certificate, "rb") as f:
                untrusted_server_creds = ssl_channel_credentials(
                    root_certificates=f.read()
                )

        connection_options = (("grpc.ssl_target_name_override", server_name),)

        try:
            channel = secure_channel(
                untrusted_client_to_enclave,
                untrusted_server_creds,
                options=connection_options,
            )
            stub = AttestationStub(channel)

            response = stub.GetServerInfo(server_info_request())
            if not supported_server_version(response.version):
                raise VersionError(
                    "Incompatible client/server versions. Please use the correct client for your server."
                )

            if self.SIMULATION_MODE:
                logging.warning(
                    "Attestation process is bypassed : running without requesting and checking attestation"
                )
                response = stub.GetCertificate(certificate_request())
                server_cert = encode_certificate(response.enclave_tls_certificate)

            else:
                self.policy = load_policy(policy)
                response = stub.GetSgxQuoteWithCollateral(quote_request())
                claims = verify_dcap_attestation(
                    response.quote, response.collateral, response.enclave_held_data
                )

                verify_claims(claims, self.policy)
                server_cert = get_server_cert(claims)

                logging.info("Quote verification passed")
                logging.info(
                    f"Certificate from attestation process\n {server_cert.decode('ascii')}"
                )
                logging.info("MREnclave\n" + claims["sgx-mrenclave"])

            channel.close()
            self.enclave_signing_key = get_enclave_signing_key(server_cert)
            server_creds = ssl_channel_credentials(root_certificates=server_cert)
            channel = secure_channel(
                attested_client_to_enclave, server_creds, options=connection_options
            )

            self.stub = ExchangeStub(channel)
            self.channel = channel
            logging.info("Successfuly connected to the server")

        except RpcError as rpc_error:
            error = ConnectionError(check_rpc_exception(rpc_error))

        finally:
            if error is not None:
                raise error

    def upload_model(
        self,
        model: str = "",
        shape: Tuple = None,
        dtype: ModelDatumType = ModelDatumType.F32,
        sign: bool = False,
        model_name: str = "default",
    ) -> UploadModelResponse:
        """Upload an inference model to the server.
        The provided model needs to be in the Onnx format.

        Args:
            model: Path to Onnx model file.
            shape: The shape of the model input.
            dtype: The type of the model input data (f32 by default)
            sign: Get signed responses from the server or not (default is False)

        Returns:
            UploadModelResponse object, containing one field:
                proof: optional, a ProofData object with two fields:
                    payload: the payload returned by the server
                    signature: the signature returned by the server
        Raises:
            ConnectionError: will be raised if the connection with the server fails.
            FileNotFoundError: will be raised if the model file is not found.
            SignatureError: will be raised if the response signature is invalid
        """
        response = None
        if dtype is None:
            dtype = ModelDatumType.F32
        if not self._is_connected():
            raise ConnectionError("Not connected to the server")

        error = None
        try:
            with open(model, "rb") as f:
                data = f.read()
            input_fact = list(shape)
            response = self.stub.SendModel(
                iter(
                    [
                        SendModelRequest(
                            length=len(data),
                            input_fact=input_fact,
                            data=chunk,
                            datum=int(dtype),
                            sign=sign,
                            model_name=model_name,
                        )
                        for chunk in create_byte_chunk(data)
                    ]
                )
            )

        except RpcError as rpc_error:
            error = ConnectionError(check_rpc_exception(rpc_error))

        finally:
            if error is not None:
                raise error

        payload = Payload.FromString(response.payload).send_model_payload
        proof_data = None
        if sign:
            try:
                self.enclave_signing_key.verify(response.signature, response.payload)
            except InvalidSignature:
                raise SignatureError("Invalid signature")
            if sha256(data).digest() != payload.model_hash:
                raise SignatureError("Invalid returned model_hash")
            if input_fact != payload.input_fact:
                raise SignatureError("Invalid returned input_fact")
            proof_data = ProofData(
                response.payload, response.signature, payload.model_id
            )

        return UploadModelResponse(proof_data, payload.model_id)

    def run_model(
        self, data_list: List[Any], sign: bool = False, model_id: str = "default"
    ) -> RunModelResponse:
        """Send data to the server to make a secure inference

        The data provided must be in a list, as the tensor will be rebuilt inside the server.

        Args:
            data_list: array of numbers, the numbers must be of the same type dtype specified in upload_model
            sign: Get signed responses from the server or not (default is False)

        Returns:
            RunModelResponse object, containing wto fields:
                proof: optional, a ProofData object with two fields:
                    payload: the payload returned by the server
                    signature: the signature returned by the server
                output: list of floats, the inference results returned by the server
        Raises:
            ConnectionError: will be raised if the connection to the server fails.
            SignatureError: will be raised if the response signature is invalid
        """

        if not self._is_connected():
            raise ConnectionError("Not connected to the server")

        error = None
        try:
            serialized_bytes = dumps(data_list)
            response = self.stub.RunModel(
                iter(
                    [
                        RunModelRequest(
                            input=serialized_bytes_chunk, sign=sign, model_id=model_id
                        )
                        for serialized_bytes_chunk in create_byte_chunk(
                            serialized_bytes
                        )
                    ]
                )
            )

        except RpcError as rpc_error:
            error = ConnectionError(check_rpc_exception(rpc_error))

        finally:
            if error is not None:
                raise error

        # Response Verification
        payload = Payload.FromString(response.payload).run_model_payload
        proof_data = None
        if sign:
            try:
                self.enclave_signing_key.verify(response.signature, response.payload)
            except InvalidSignature:
                raise SignatureError("Invalid signature")
            if sha256(serialized_bytes).digest() != payload.input_hash:
                raise SignatureError("Invalid returned input_hash")
            if (model_id != "default") and (payload.model_id != model_id):
                raise SignatureError("Invalid model")

            proof_data = ProofData(
                response.payload, response.signature, payload.model_id
            )

        # Get the output of inference
        return RunModelResponse(proof_data, payload.output)

    def delete_model(self, model_id, sign=False) -> DeleteModelResponse:
        if not self._is_connected():
            raise ConnectionError("Not connected to the server")

        error = None
        try:
            response = self.stub.DeleteModel(
                DeleteModelRequest(model_id=model_id.encode(), sign=sign)
            )

        except RpcError as rpc_error:
            error = ConnectionError(check_rpc_exception(rpc_error))

        finally:
            if error is not None:
                raise error

        # Response Verification
        payload = Payload.FromString(response.payload).delete_model_payload
        proof_data = None
        if sign:
            try:
                self.enclave_signing_key.verify(response.signature, response.payload)
            except InvalidSignature:
                raise SignatureError("Invalid signature")
            if payload.model_id != model_id:
                raise SignatureError("Invalid model")

            proof_data = ProofData(
                response.payload, response.signature, payload.model_id
            )

        # Get the output of inference
        return DeleteModelResponse(proof_data)

    def close_connection(self):
        """Close the connection between the client and the inference server."""
        if self._is_connected():
            self._close_channel()
            self.channel = None
            self.stub = None
            self.policy = None
