// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

/// @title StoreRegistry
/// @notice Minimal on-chain registry binding a Tilla store slug to its merchant
///         wallet and a content metadata hash. Owner-gated (the Tilla registrar,
///         set once in the constructor). Holds no funds, has no payable paths, and
///         takes no custody — it is a public, verifiable index only.
/// @dev    storeId = keccak256(bytes(slug)). One record per slug; register is
///         one-shot, metadata is updatable. PREPARED, NOT DEPLOYED — deploying is an
///         on-chain contract creation (gas + key) and is a user-gated runbook step.
contract StoreRegistry {
    struct StoreRec {
        address merchant;
        bytes32 metadataHash;
        uint64 registeredAt;
    }

    /// @notice The Tilla registrar; the only address allowed to register/update.
    address public immutable owner;

    /// @notice storeId (keccak256(slug)) => record.
    mapping(bytes32 => StoreRec) public stores;

    event StoreRegistered(
        bytes32 indexed storeId, string slug, address indexed merchant, bytes32 metadataHash
    );
    event StoreMetadataUpdated(bytes32 indexed storeId, string slug, bytes32 metadataHash);

    error NotOwner();
    error ZeroAddress();
    error AlreadyRegistered();
    error NotRegistered();

    modifier onlyOwner() {
        if (msg.sender != owner) revert NotOwner();
        _;
    }

    constructor(address registrar) {
        if (registrar == address(0)) revert ZeroAddress();
        owner = registrar;
    }

    /// @notice The deterministic storeId for a slug.
    function storeId(string calldata slug) public pure returns (bytes32) {
        return keccak256(bytes(slug));
    }

    /// @notice Register a store once. Reverts if the slug is already registered or
    ///         the merchant is the zero address.
    function register(string calldata slug, address merchant, bytes32 metadataHash)
        external
        onlyOwner
    {
        if (merchant == address(0)) revert ZeroAddress();
        bytes32 id = keccak256(bytes(slug));
        if (stores[id].merchant != address(0)) revert AlreadyRegistered();
        stores[id] =
            StoreRec({merchant: merchant, metadataHash: metadataHash, registeredAt: uint64(block.timestamp)});
        emit StoreRegistered(id, slug, merchant, metadataHash);
    }

    /// @notice Update a registered store's metadata hash (e.g. after a content edit).
    function updateMetadata(string calldata slug, bytes32 metadataHash) external onlyOwner {
        bytes32 id = keccak256(bytes(slug));
        if (stores[id].merchant == address(0)) revert NotRegistered();
        stores[id].metadataHash = metadataHash;
        emit StoreMetadataUpdated(id, slug, metadataHash);
    }
}
